import io
import os
import re
from datetime import datetime

import pandas as pd
import psycopg2
import streamlit as st
from psycopg2.extras import execute_batch, execute_values
from psycopg2.pool import ThreadedConnectionPool
from streamlit_cookies_manager import EncryptedCookieManager

# =========================================================
# CONFIG STREAMLIT
# =========================================================
st.set_page_config(page_title="Controle de Cartões", layout="wide")

# =========================================================
# FUNÇÕES UTILITÁRIAS
# =========================================================
def agora_str():
    return datetime.now().strftime("%d-%m-%Y %H:%M:%S")


def formatar_data(valor):
    if pd.isna(valor) or valor is None:
        return None
    v = str(valor).strip()
    if v == "":
        return None
    try:
        dt = pd.to_datetime(v, dayfirst=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.strftime("%d-%m-%Y")
    except Exception:
        return None


def normalizar_mes_referencia(valor):
    """Aceita 03.2026, 03/2026 ou 03-2026 e devolve 03-2026."""
    if valor is None:
        return None
    v = str(valor).strip().replace('.', '-').replace('/', '-')
    m = re.fullmatch(r"(\d{1,2})-(\d{4})", v)
    if not m:
        return None
    mes = int(m.group(1))
    ano = int(m.group(2))
    if mes < 1 or mes > 12:
        return None
    return f"{mes:02d}-{ano:04d}"


def normalizar_matricula(valor):
    if valor is None or pd.isna(valor):
        return ""
    v = str(valor).strip()
    if v.endswith('.0') and v[:-2].isdigit():
        v = v[:-2]
    return v


def normalizar_numero_caixa(valor):
    if valor is None or pd.isna(valor):
        return ""
    v = str(valor).strip()
    if v.endswith('.0') and v[:-2].isdigit():
        v = v[:-2]
    return v


def registrar_log(cur, usuario, acao, detalhe):
    cur.execute(
        """
        INSERT INTO logs (usuario, acao, detalhe, data)
        VALUES (%s,%s,%s,%s)
        """,
        (usuario, acao, detalhe, agora_str()),
    )


def registrar_logs_em_lote(cur, usuario, acao, detalhes):
    if not detalhes:
        return
    ts = agora_str()
    registros = [(usuario, acao, d, ts) for d in detalhes]
    execute_values(
        cur,
        "INSERT INTO logs (usuario, acao, detalhe, data) VALUES %s",
        registros,
        page_size=1000,
    )


# =========================================================
# SESSÃO / MEMÓRIA
# =========================================================
if "usuario_logado" not in st.session_state:
    st.session_state.usuario_logado = None
    st.session_state.perfil = None

if "memoria" not in st.session_state:
    st.session_state.memoria = {
        "mes_gestao": None,
        "caixa_gestao": None,
        "contrato_gestao": None,
        "mes_consulta": None,
        "mes_auditoria": None,
    }

# =========================================================
# BANCO (POOL)
# =========================================================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    st.error("DATABASE_URL não encontrada (configure nas variáveis do Streamlit Cloud).")
    st.stop()


@st.cache_resource
def get_pool():
    return ThreadedConnectionPool(
        minconn=1,
        maxconn=10,  # se tiver muitos usuários simultâneos, suba para 20
        dsn=DATABASE_URL,
    )


def get_conn_cursor():
    pool = get_pool()
    conn = pool.getconn()

    # limpa transação pendente/aborted
    try:
        conn.rollback()
    except Exception:
        pass

    try:
        conn.autocommit = False
    except Exception:
        pass

    cur = conn.cursor()
    return pool, conn, cur


def close_conn(pool, conn, cur=None, commit=True):
    try:
        if conn is not None:
            if commit:
                conn.commit()
            else:
                conn.rollback()
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        try:
            if pool is not None and conn is not None:
                pool.putconn(conn)
        except Exception:
            pass


def sql_df(query, params=None):
    pool = conn = cur = None
    try:
        pool, conn, cur = get_conn_cursor()
        df = pd.read_sql(query, conn, params=params)
        close_conn(pool, conn, cur, commit=True)
        return df
    except Exception:
        close_conn(pool, conn, cur, commit=False)
        raise


def sql_exec(query, params=None):
    pool = conn = cur = None
    try:
        pool, conn, cur = get_conn_cursor()
        cur.execute(query, params)
        close_conn(pool, conn, cur, commit=True)
    except Exception:
        close_conn(pool, conn, cur, commit=False)
        raise


def buscar_colaboradores(termo, limite=150):
    termo = (termo or "").strip()
    if len(termo) < 2:
        return pd.DataFrame(columns=["matricula", "nome", "contrato"])

    return sql_df(
        """
        SELECT matricula, nome, contrato
        FROM base_colaboradores
        WHERE matricula ILIKE %s OR nome ILIKE %s
        ORDER BY nome
        LIMIT %s
        """,
        params=(f"%{termo}%", f"%{termo}%", limite),
    )


def listar_contratos():
    df = sql_df(
        """
        SELECT DISTINCT contrato
        FROM base_colaboradores
        WHERE contrato IS NOT NULL AND BTRIM(contrato) <> ''
        ORDER BY contrato
        """
    )
    return df["contrato"].dropna().tolist() if not df.empty else []


def resumo_base():
    return sql_df(
        """
        SELECT COUNT(*) AS total, MAX(ultima_atualizacao) AS ultima_atualizacao
        FROM base_colaboradores
        """
    )


@st.cache_data(show_spinner=False)
def analisar_planilha_historica(conteudo_arquivo):
    """Analisa o modelo histórico sem gravar nada no banco."""
    xls = pd.ExcelFile(io.BytesIO(conteudo_arquivo), engine="openpyxl")
    abas_mensais = [
        aba for aba in xls.sheet_names
        if re.fullmatch(r"\d{2}[.-]\d{4}", str(aba).strip())
    ]

    mapa_localizacao = {}
    if "CAIXAS" in xls.sheet_names:
        try:
            df_caixas = pd.read_excel(xls, sheet_name="CAIXAS", dtype=str)
            if not df_caixas.empty:
                df_caixas.columns = [
                    str(c).strip().lower().replace('*', '').strip()
                    for c in df_caixas.columns
                ]
                if {"mes_referencia", "numero_caixa"}.issubset(df_caixas.columns):
                    for _, r in df_caixas.iterrows():
                        mes = normalizar_mes_referencia(r.get("mes_referencia"))
                        caixa = normalizar_numero_caixa(r.get("numero_caixa"))
                        loc = "" if pd.isna(r.get("localizacao")) else str(r.get("localizacao")).strip()
                        if mes and caixa:
                            mapa_localizacao[(mes, caixa)] = loc
        except Exception:
            pass

    resumo = []
    for aba in abas_mensais:
        mes_ref = normalizar_mes_referencia(aba)
        try:
            df = pd.read_excel(
                xls, sheet_name=aba, header=2, usecols="A:F", dtype=str
            )
        except Exception:
            resumo.append({
                "aba": aba, "mes": mes_ref or aba, "registros": 0,
                "caixas": 0, "incompletos": 0, "duplicados": 0, "localizacoes_ausentes": 0,
                "erro": "Não foi possível ler a aba"
            })
            continue

        if df.shape[1] < 4:
            resumo.append({
                "aba": aba, "mes": mes_ref or aba, "registros": 0,
                "caixas": 0, "incompletos": 0, "duplicados": 0, "localizacoes_ausentes": 0,
                "erro": "Estrutura inesperada"
            })
            continue

        cols = ["matricula", "nome", "contrato", "numero_caixa", "localizacao", "status"]
        while df.shape[1] < len(cols):
            df[f"_extra_{df.shape[1]}"] = None
        df = df.iloc[:, :6].copy()
        df.columns = cols
        df["matricula"] = df["matricula"].apply(normalizar_matricula)
        df["numero_caixa"] = df["numero_caixa"].apply(normalizar_numero_caixa)

        tem_mat = df["matricula"].ne("")
        tem_caixa = df["numero_caixa"].ne("")
        validos = df[tem_mat & tem_caixa].copy()
        incompletos = int((tem_mat ^ tem_caixa).sum())
        duplicados = int(validos.duplicated(subset=["matricula"], keep="first").sum())
        validos_unicos = validos.drop_duplicates(subset=["matricula"], keep="first").copy()

        caixas = sorted(validos_unicos["numero_caixa"].dropna().unique().tolist())
        loc_ausentes = sum(
            1 for cx in caixas if not mapa_localizacao.get((mes_ref, cx), "")
        )

        resumo.append({
            "aba": aba,
            "mes": mes_ref or aba,
            "registros": int(len(validos_unicos)),
            "duplicados": duplicados,
            "caixas": int(len(caixas)),
            "incompletos": incompletos,
            "localizacoes_ausentes": int(loc_ausentes),
            "erro": "",
        })

    return pd.DataFrame(resumo)


def carregar_dados_aba_historica(conteudo_arquivo, aba):
    xls = pd.ExcelFile(io.BytesIO(conteudo_arquivo), engine="openpyxl")
    df = pd.read_excel(xls, sheet_name=aba, header=2, usecols="A:F", dtype=str)
    cols = ["matricula", "nome", "contrato", "numero_caixa", "localizacao", "status"]
    while df.shape[1] < len(cols):
        df[f"_extra_{df.shape[1]}"] = None
    df = df.iloc[:, :6].copy()
    df.columns = cols
    df["matricula"] = df["matricula"].apply(normalizar_matricula)
    df["numero_caixa"] = df["numero_caixa"].apply(normalizar_numero_caixa)
    df = df[(df["matricula"] != "") & (df["numero_caixa"] != "")].copy()
    # O banco aceita somente uma matrícula por mês. Duplicidades da planilha são ignoradas, mantendo a primeira.
    df = df.drop_duplicates(subset=["matricula"], keep="first").copy()
    df["nome"] = df["nome"].fillna("").astype(str).str.strip()
    df["contrato"] = df["contrato"].fillna("").astype(str).str.strip()
    return df


def carregar_mapa_localizacoes(conteudo_arquivo):
    mapa = {}
    try:
        xls = pd.ExcelFile(io.BytesIO(conteudo_arquivo), engine="openpyxl")
        if "CAIXAS" not in xls.sheet_names:
            return mapa
        df = pd.read_excel(xls, sheet_name="CAIXAS", dtype=str)
        df.columns = [str(c).strip().lower().replace('*', '').strip() for c in df.columns]
        if not {"mes_referencia", "numero_caixa"}.issubset(df.columns):
            return mapa
        for _, r in df.iterrows():
            mes = normalizar_mes_referencia(r.get("mes_referencia"))
            caixa = normalizar_numero_caixa(r.get("numero_caixa"))
            loc = "" if pd.isna(r.get("localizacao")) else str(r.get("localizacao")).strip()
            if mes and caixa:
                mapa[(mes, caixa)] = loc
    except Exception:
        pass
    return mapa


# =========================================================
# COOKIES
# =========================================================
cookies = EncryptedCookieManager(prefix="controle_cartoes_", password="senha_super_secreta")
if not cookies.ready():
    st.stop()

# =========================================================
# MIGRAÇÕES / TABELAS / ÍNDICES
# =========================================================
def run_migrations():
    pool, conn, cur = get_conn_cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS base_colaboradores (
                id SERIAL PRIMARY KEY,
                matricula TEXT UNIQUE,
                nome TEXT,
                contrato TEXT,
                responsavel TEXT,
                data_admissao TEXT,
                data_demissao TEXT,
                sit_folha TEXT,
                ultima_atualizacao TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meses (
                id SERIAL PRIMARY KEY,
                mes_referencia TEXT UNIQUE
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS caixas (
                id SERIAL PRIMARY KEY,
                numero_caixa TEXT,
                mes_id INTEGER,
                localizacao TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cartoes_ponto (
                id SERIAL PRIMARY KEY,
                matricula TEXT,
                caixa_id INTEGER,
                mes_id INTEGER,
                data_registro TEXT,
                UNIQUE (matricula, mes_id)
            )
            """
        )

        # colunas histórico/status
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ARQUIVADO'")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS data_desarquivamento TEXT")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS usuario_desarquivou TEXT")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS motivo_desarquivamento TEXT")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS origem TEXT DEFAULT 'MANUAL'")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS arquivo_origem TEXT")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS aba_origem TEXT")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS nome_historico TEXT")
        cur.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS contrato_historico TEXT")
        cur.execute("UPDATE cartoes_ponto SET origem='MANUAL' WHERE origem IS NULL OR BTRIM(origem)=''")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                password TEXT,
                perfil TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                usuario TEXT,
                acao TEXT,
                detalhe TEXT,
                data TEXT
            )
            """
        )

        # índices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_base_matricula ON base_colaboradores(matricula)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_mes ON cartoes_ponto(mes_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_matricula ON cartoes_ponto(matricula)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_caixas_mes ON caixas(mes_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_mes_status ON cartoes_ponto(mes_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_caixa_status ON cartoes_ponto(caixa_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_base_contrato ON base_colaboradores(contrato)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_origem ON cartoes_ponto(origem)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_caixas_mes_numero ON caixas(mes_id, numero_caixa)")

        close_conn(pool, conn, cur, commit=True)
    except Exception as e:
        close_conn(pool, conn, cur, commit=False)
        st.error(f"Erro nas migrações/tabelas: {e}")
        st.stop()


def ensure_admin():
    pool, conn, cur = get_conn_cursor()
    try:
        cur.execute("SELECT 1 FROM usuarios WHERE username=%s", ("adm",))
        existe = cur.fetchone()
        if not existe:
            cur.execute(
                "INSERT INTO usuarios (username, password, perfil) VALUES (%s,%s,%s)",
                ("adm", "123", "admin"),
            )
        close_conn(pool, conn, cur, commit=True)
    except Exception as e:
        close_conn(pool, conn, cur, commit=False)
        st.error(f"Erro ao garantir admin padrão: {e}")
        st.stop()


@st.cache_resource
def inicializar_banco():
    run_migrations()
    ensure_admin()
    return True


inicializar_banco()

# =========================================================
# AUTO LOGIN
# =========================================================
if st.session_state.usuario_logado is None:
    try:
        user_cookie = cookies.get("usuario")
    except Exception:
        user_cookie = None

    if user_cookie:
        try:
            df_u = sql_df("SELECT username, perfil FROM usuarios WHERE username=%s", params=(user_cookie,))
            if not df_u.empty:
                st.session_state.usuario_logado = df_u.iloc[0]["username"]
                st.session_state.perfil = df_u.iloc[0]["perfil"]
        except Exception:
            # ignora cookie se falhar
            pass

# =========================================================
# LOGIN
# =========================================================
if st.session_state.usuario_logado is None:
    st.title("🔐 Login do Sistema")

    user = st.text_input("Usuário", key="login_user")
    senha = st.text_input("Senha", type="password", key="login_pass")
    manter = st.checkbox("Manter conectado", key="login_keep")

    if st.button("Entrar", key="login_btn"):
        pool, conn, cur = get_conn_cursor()
        try:
            cur.execute(
                "SELECT username, perfil FROM usuarios WHERE username=%s AND password=%s",
                (user, senha),
            )
            usuario = cur.fetchone()
            close_conn(pool, conn, cur, commit=True)

            if usuario:
                st.session_state.usuario_logado = usuario[0]
                st.session_state.perfil = usuario[1]
                if manter:
                    cookies["usuario"] = usuario[0]
                    cookies.save()
                st.success("Login realizado!")
                st.rerun()
            else:
                st.error("Usuário ou senha inválidos.")
        except Exception as e:
            close_conn(pool, conn, cur, commit=False)
            st.error(f"Erro no login: {e}")

    st.stop()

# =========================================================
# MENU
# =========================================================
menu = st.sidebar.radio(
    "Menu",
    [
        "Importar Base Excel",
        "Importar Histórico",
        "Visualizar Base",
        "Gestão de Caixas",
        "Consultar Arquivamentos",
        "Auditoria",
        "Gestão de Usuários",
    ],
    key="menu_principal",
)

if st.sidebar.button("🚪 Sair", key="btn_logout"):
    st.session_state.usuario_logado = None
    st.session_state.perfil = None
    cookies["usuario"] = ""
    cookies.save()
    st.rerun()

# =========================================================
# IMPORTAÇÃO BASE
# =========================================================
if menu == "Importar Base Excel":
    if st.session_state.perfil != "admin":
        st.error("Apenas administradores podem alterar a base.")
        st.stop()

    st.header("📊 Importar / Atualizar Base de Colaboradores")
    st.info("⚠ Datas devem estar no formato DD-MM-YYYY (ou datas reconhecíveis pelo Excel).")

    try:
        rb = resumo_base()
        if not rb.empty:
            total_base = int(rb.iloc[0]["total"] or 0)
            ultima_base = rb.iloc[0]["ultima_atualizacao"]
            st.caption(f"Base atual: {total_base:,} colaborador(es) • Última atualização: {ultima_base or 'sem registro'}".replace(',', '.'))
    except Exception:
        pass

    arquivo = st.file_uploader("Envie a planilha (.xlsx)", type=["xlsx"], key="upl_base")

    if arquivo is not None:
        try:
            df = pd.read_excel(arquivo, dtype=str)
        except Exception:
            st.error("Erro ao ler o arquivo.")
            st.stop()

        df.columns = df.columns.str.strip().str.lower()

        obrigatorias = [
            "matricula",
            "nome",
            "contrato",
            "responsavel",
            "data_admissao",
            "data_demissao",
            "sit_folha",
        ]

        if not all(col in df.columns for col in obrigatorias):
            st.error("❌ A planilha não está no formato correto.")
            st.write("Colunas obrigatórias:", obrigatorias)
            st.stop()

        df["matricula"] = df["matricula"].apply(normalizar_matricula)
        df["data_admissao"] = df["data_admissao"].apply(formatar_data)
        df["data_demissao"] = df["data_demissao"].apply(formatar_data)
        ultima = agora_str()

        registros = [
            (
                r["matricula"],
                r["nome"],
                r["contrato"],
                r["responsavel"],
                r["data_admissao"],
                r["data_demissao"],
                r["sit_folha"],
                ultima,
            )
            for _, r in df.iterrows()
            if str(r["matricula"]).strip() != ""
        ]

        query = """
        INSERT INTO base_colaboradores
        (matricula, nome, contrato, responsavel, data_admissao, data_demissao, sit_folha, ultima_atualizacao)
        VALUES %s
        ON CONFLICT (matricula)
        DO UPDATE SET
            nome = EXCLUDED.nome,
            contrato = EXCLUDED.contrato,
            responsavel = EXCLUDED.responsavel,
            data_admissao = EXCLUDED.data_admissao,
            data_demissao = EXCLUDED.data_demissao,
            sit_folha = EXCLUDED.sit_folha,
            ultima_atualizacao = EXCLUDED.ultima_atualizacao
        """

        pool = conn = cur = None
        try:
            pool, conn, cur = get_conn_cursor()
            execute_values(cur, query, registros, page_size=2000)
            close_conn(pool, conn, cur, commit=True)
            st.success(f"✅ Importação concluída: {len(registros)} registro(s) processado(s).")
        except Exception as e:
            close_conn(pool, conn, cur, commit=False)
            st.error(f"Erro na importação: {e}")

# =========================================================
# IMPORTAÇÃO HISTÓRICA
# =========================================================
if menu == "Importar Histórico":
    if st.session_state.perfil != "admin":
        st.error("Apenas administradores podem importar histórico.")
        st.stop()

    st.header("📥 Importar Histórico de Cartões")
    st.info(
        "Esta opção NÃO substitui a base atual de colaboradores. "
        "Ela migra os registros das abas mensais para o histórico do sistema."
    )
    st.caption(
        "Regra usada: toda linha com matrícula + número da caixa será importada como ARQUIVADO, "
        "com origem IMPORTACAO. Registros já existentes para a mesma matrícula e mês são preservados."
    )

    arquivo_hist = st.file_uploader(
        "Envie a planilha histórica (.xlsx)", type=["xlsx"], key="upl_historico"
    )

    if arquivo_hist is not None:
        conteudo_hist = arquivo_hist.getvalue()
        try:
            resumo_hist = analisar_planilha_historica(conteudo_hist)
        except Exception as e:
            st.error(f"Não foi possível analisar a planilha: {e}")
            st.stop()

        if resumo_hist.empty:
            st.warning("Nenhuma aba mensal no padrão MM.AAAA ou MM-AAAA foi encontrada.")
            st.stop()

        st.subheader("Prévia da importação")
        st.dataframe(
            resumo_hist[[
                "aba", "mes", "registros", "duplicados", "caixas", "incompletos", "localizacoes_ausentes", "erro"
            ]],
            use_container_width=True,
            hide_index=True,
        )

        total_registros_hist = int(resumo_hist["registros"].sum())
        total_incompletos_hist = int(resumo_hist["incompletos"].sum())
        total_duplicados_hist = int(resumo_hist["duplicados"].sum())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Abas mensais", len(resumo_hist))
        c2.metric("Registros válidos", total_registros_hist)
        c3.metric("Duplicados ignorados", total_duplicados_hist)
        c4.metric("Linhas incompletas", total_incompletos_hist)

        abas_validas = resumo_hist.loc[resumo_hist["erro"] == "", "aba"].tolist()
        abas_selecionadas = st.multiselect(
            "Abas que serão importadas",
            abas_validas,
            default=abas_validas,
            key="hist_abas_selecionadas",
        )

        confirmar = st.checkbox(
            "Confirmo que revisei a prévia e desejo importar os registros selecionados.",
            key="hist_confirmar",
        )

        if st.button(
            "📥 Importar histórico selecionado",
            type="primary",
            disabled=not confirmar or not abas_selecionadas,
            key="btn_importar_historico",
        ):
            mapa_localizacoes = carregar_mapa_localizacoes(conteudo_hist)
            resultados = []
            barra = st.progress(0)

            for pos, aba in enumerate(abas_selecionadas, start=1):
                mes_ref = normalizar_mes_referencia(aba)
                novos = 0
                existentes = 0
                erros = 0
                caixas_criadas = 0
                pool = conn = cur = None

                try:
                    dados = carregar_dados_aba_historica(conteudo_hist, aba)
                    pool, conn, cur = get_conn_cursor()

                    # 1) Garante o mês
                    cur.execute(
                        "INSERT INTO meses (mes_referencia) VALUES (%s) ON CONFLICT (mes_referencia) DO NOTHING",
                        (mes_ref,),
                    )
                    cur.execute("SELECT id FROM meses WHERE mes_referencia=%s", (mes_ref,))
                    mes_id = int(cur.fetchone()[0])

                    # 2) Localiza/cria caixas usadas na aba
                    caixas_ids = {}
                    for numero_caixa in sorted(dados["numero_caixa"].unique().tolist()):
                        cur.execute(
                            "SELECT id, localizacao FROM caixas WHERE mes_id=%s AND numero_caixa=%s ORDER BY id LIMIT 1",
                            (mes_id, numero_caixa),
                        )
                        achou = cur.fetchone()
                        loc_planilha = mapa_localizacoes.get((mes_ref, numero_caixa), "")
                        if not loc_planilha:
                            locs_aba = dados.loc[dados["numero_caixa"] == numero_caixa, "localizacao"].dropna().astype(str).str.strip()
                            locs_aba = locs_aba[locs_aba != ""]
                            if not locs_aba.empty:
                                loc_planilha = locs_aba.iloc[0]

                        if achou:
                            caixa_id = int(achou[0])
                            loc_atual = achou[1] or ""
                            if loc_planilha and not str(loc_atual).strip():
                                cur.execute(
                                    "UPDATE caixas SET localizacao=%s WHERE id=%s",
                                    (loc_planilha, caixa_id),
                                )
                        else:
                            cur.execute(
                                "INSERT INTO caixas (numero_caixa, mes_id, localizacao) VALUES (%s,%s,%s) RETURNING id",
                                (numero_caixa, mes_id, loc_planilha),
                            )
                            caixa_id = int(cur.fetchone()[0])
                            caixas_criadas += 1

                        caixas_ids[numero_caixa] = caixa_id

                    # 3) Identifica o que já existe antes de inserir
                    mats = dados["matricula"].astype(str).tolist()
                    existentes_set = set()
                    if mats:
                        cur.execute(
                            "SELECT matricula FROM cartoes_ponto WHERE mes_id=%s AND matricula = ANY(%s)",
                            (mes_id, mats),
                        )
                        existentes_set = {str(r[0]) for r in cur.fetchall()}

                    registros_novos = []
                    ts = agora_str()
                    for _, r in dados.iterrows():
                        mat = str(r["matricula"])
                        if mat in existentes_set:
                            existentes += 1
                            continue
                        caixa_id = caixas_ids.get(r["numero_caixa"])
                        if not caixa_id:
                            erros += 1
                            continue
                        registros_novos.append((
                            mat,
                            caixa_id,
                            mes_id,
                            ts,
                            "ARQUIVADO",
                            "IMPORTACAO",
                            arquivo_hist.name,
                            aba,
                            r.get("nome", ""),
                            r.get("contrato", ""),
                        ))

                    if registros_novos:
                        execute_values(
                            cur,
                            """
                            INSERT INTO cartoes_ponto
                            (matricula, caixa_id, mes_id, data_registro, status, origem,
                             arquivo_origem, aba_origem, nome_historico, contrato_historico)
                            VALUES %s
                            ON CONFLICT (matricula, mes_id) DO NOTHING
                            """,
                            registros_novos,
                            page_size=2000,
                        )
                        novos = len(registros_novos)

                    registrar_log(
                        cur,
                        st.session_state.usuario_logado,
                        "IMPORTACAO_HISTORICA",
                        (
                            f"Arquivo {arquivo_hist.name} | Aba {aba} | Mes {mes_ref} | "
                            f"Novos {novos} | Existentes {existentes} | Erros {erros} | "
                            f"Caixas criadas {caixas_criadas}"
                        ),
                    )

                    close_conn(pool, conn, cur, commit=True)
                    pool = conn = cur = None
                except Exception as e:
                    if pool is not None or conn is not None:
                        close_conn(pool, conn, cur, commit=False)
                    erros += 1
                    resultados.append({
                        "aba": aba,
                        "mes": mes_ref,
                        "novos": novos,
                        "já existentes": existentes,
                        "caixas criadas": caixas_criadas,
                        "erros": erros,
                        "detalhe": str(e),
                    })
                    barra.progress(pos / len(abas_selecionadas))
                    continue

                resultados.append({
                    "aba": aba,
                    "mes": mes_ref,
                    "novos": novos,
                    "já existentes": existentes,
                    "caixas criadas": caixas_criadas,
                    "erros": erros,
                    "detalhe": "OK",
                })
                barra.progress(pos / len(abas_selecionadas))

            st.success("Importação histórica finalizada.")
            st.dataframe(pd.DataFrame(resultados), use_container_width=True, hide_index=True)
            st.caption(
                "Observação: data_registro representa a data da migração para o app. "
                "A planilha não contém a data original em que o cartão foi fisicamente arquivado."
            )


# =========================================================
# VISUALIZAR BASE
# =========================================================
if menu == "Visualizar Base":
    st.header("📋 Base Atual no Sistema")

    f1, f2, f3 = st.columns([2, 2, 1])
    busca_base = f1.text_input("Buscar por nome ou matrícula", key="base_busca")
    contratos_base = ["Todos"] + listar_contratos()
    contrato_base = f2.selectbox("Contrato", contratos_base, key="base_contrato")
    tamanho_pagina = f3.selectbox("Registros por página", [100, 250, 500], index=1, key="base_page_size")

    where = ["1=1"]
    params = []
    if busca_base.strip():
        where.append("(nome ILIKE %s OR matricula ILIKE %s)")
        termo = f"%{busca_base.strip()}%"
        params.extend([termo, termo])
    if contrato_base != "Todos":
        where.append("contrato=%s")
        params.append(contrato_base)

    where_sql = " AND ".join(where)
    total_df = sql_df(
        f"SELECT COUNT(*) AS total FROM base_colaboradores WHERE {where_sql}",
        tuple(params) if params else None,
    )
    total = int(total_df.iloc[0]["total"] if not total_df.empty else 0)
    paginas = max(1, (total + tamanho_pagina - 1) // tamanho_pagina)
    pagina = st.number_input("Página", min_value=1, max_value=paginas, value=1, step=1, key="base_pagina")
    offset = (int(pagina) - 1) * tamanho_pagina

    query = f"""
        SELECT id, matricula, nome, contrato, responsavel, data_admissao,
               data_demissao, sit_folha, ultima_atualizacao
        FROM base_colaboradores
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT %s OFFSET %s
    """
    params_lista = list(params) + [tamanho_pagina, offset]
    df = sql_df(query, tuple(params_lista))

    st.caption(f"Exibindo {len(df)} registro(s) nesta página • Total encontrado: {total}")
    if df.empty:
        st.warning("Nenhum registro encontrado.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

# =========================================================
# GESTÃO DE CAIXAS
# =========================================================
if menu == "Gestão de Caixas":
    st.header("📦 Gestão de Caixas")

    abas = st.tabs(["Criar Mês", "Criar Caixa", "Operações (Arquivar/Desarquivar/Excluir)"])

    # -------------------------
    # CRIAR MÊS
    # -------------------------
    with abas[0]:
        st.subheader("📅 Criar Mês")
        mes = st.text_input("Mês referência (ex: 01-2026)", key="criar_mes_txt")

        if st.button("Salvar Mês", key="criar_mes_btn"):
            mes_ok = normalizar_mes_referencia(mes)
            if not mes_ok:
                st.warning("Informe um mês válido, por exemplo 01-2026, 01/2026 ou 01.2026.")
            else:
                pool = conn = cur = None
                try:
                    pool, conn, cur = get_conn_cursor()
                    cur.execute("INSERT INTO meses (mes_referencia) VALUES (%s)", (mes_ok,))
                    close_conn(pool, conn, cur, commit=True)
                    st.success("Mês criado!")
                    st.rerun()
                except Exception as e:
                    close_conn(pool, conn, cur, commit=False)
                    st.error(f"Mês já existe ou valor inválido. Detalhe: {e}")

    # -------------------------
    # CRIAR CAIXA
    # -------------------------
    with abas[1]:
        st.subheader("📦 Criar Caixa")
        meses = sql_df("SELECT * FROM meses ORDER BY id DESC")
        if meses.empty:
            st.warning("Cadastre um mês primeiro.")
        else:
            mes_id = st.selectbox(
                "Mês",
                meses["id"].tolist(),
                format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0],
                key="criar_caixa_mes",
            )
            numero = st.text_input("Número da Caixa", key="criar_caixa_num")
            local = st.text_input("Localização", key="criar_caixa_local")

            if st.button("Criar Caixa", key="criar_caixa_btn"):
                if not str(numero).strip():
                    st.warning("Informe o número da caixa.")
                else:
                    pool = conn = cur = None
                    try:
                        pool, conn, cur = get_conn_cursor()
                        cur.execute(
                            "INSERT INTO caixas (numero_caixa, mes_id, localizacao) VALUES (%s,%s,%s)",
                            (numero.strip(), int(mes_id), (local or "").strip()),
                        )
                        close_conn(pool, conn, cur, commit=True)
                        st.success("Caixa criada!")
                        st.rerun()
                    except Exception as e:
                        close_conn(pool, conn, cur, commit=False)
                        st.error(f"Erro ao criar caixa: {e}")

    # -------------------------
    # OPERAÇÕES
    # -------------------------
    with abas[2]:
        st.subheader("📌 Operações")

        meses = sql_df("SELECT * FROM meses ORDER BY id DESC")
        if meses.empty:
            st.warning("Cadastre um mês primeiro.")
            st.stop()

        acao = st.selectbox(
            "O que você deseja fazer?",
            ["Arquivar cartões", "Desarquivar (retirar cartão)", "Excluir Caixa", "Excluir Mês"],
            key="acao_gestao_unica",
        )

        st.divider()

        # =========================================================
        # 1) ARQUIVAR
        # =========================================================
        if acao == "Arquivar cartões":
            st.caption("Selecione mês e caixa. Depois selecione por contrato OU busque direto por funcionário.")

            meses_ids = meses["id"].tolist()
            idx_mes = 0
            if st.session_state.memoria.get("mes_gestao") in meses_ids:
                idx_mes = meses_ids.index(st.session_state.memoria.get("mes_gestao"))

            mes_id = st.selectbox(
                "Mês de referência",
                meses_ids,
                index=idx_mes,
                format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0],
                key="arq_mes",
            )
            st.session_state.memoria["mes_gestao"] = mes_id

            caixas_mes = sql_df(
                "SELECT * FROM caixas WHERE mes_id=%s ORDER BY numero_caixa",
                params=(int(mes_id),),
            )
            if caixas_mes.empty:
                st.warning("Nenhuma caixa cadastrada para este mês. Vá em **Criar Caixa**.")
                st.stop()

            caixas_ids = caixas_mes["id"].tolist()
            idx_caixa = 0
            if st.session_state.memoria.get("caixa_gestao") in caixas_ids:
                idx_caixa = caixas_ids.index(st.session_state.memoria.get("caixa_gestao"))

            caixa_id = st.selectbox(
                "Caixa de destino",
                caixas_ids,
                index=idx_caixa,
                format_func=lambda x: (
                    f"Caixa {caixas_mes.loc[caixas_mes['id']==x,'numero_caixa'].values[0]} • "
                    f"{caixas_mes.loc[caixas_mes['id']==x,'localizacao'].values[0]}"
                ),
                key="arq_caixa",
            )
            st.session_state.memoria["caixa_gestao"] = caixa_id

            st.divider()

            modo = st.radio(
                "Modo de seleção",
                ["Por contrato", "Direto por funcionário (buscar)"],
                horizontal=True,
                key="modo_selecao_arq",
            )

            selecionados_matriculas = []

            if modo == "Por contrato":
                contratos_lista = listar_contratos()
                if not contratos_lista:
                    st.warning("Base de colaboradores vazia. Importe a base primeiro.")
                    st.stop()

                idx_contrato = 0
                if st.session_state.memoria.get("contrato_gestao") in contratos_lista:
                    idx_contrato = contratos_lista.index(st.session_state.memoria.get("contrato_gestao"))

                contrato = st.selectbox(
                    "Contrato (alocação)",
                    contratos_lista,
                    index=idx_contrato,
                    key="arq_contrato",
                )
                st.session_state.memoria["contrato_gestao"] = contrato

                funcionarios = sql_df(
                    "SELECT matricula, nome FROM base_colaboradores WHERE contrato=%s ORDER BY matricula",
                    params=(contrato,),
                )

                selecionados_matriculas = st.multiselect(
                    "Selecione os funcionários",
                    funcionarios["matricula"].tolist(),
                    format_func=lambda m: f"{m} | {funcionarios.loc[funcionarios['matricula']==m,'nome'].values[0]} | {contrato}",
                    key="arq_multi_contrato",
                )
            else:
                termo = st.text_input("Digite parte do nome ou matrícula (mínimo 2 caracteres)", key="busca_func")
                df_busca = buscar_colaboradores(termo)

                if len(termo.strip()) >= 2 and not df_busca.empty:
                    opcoes = []
                    mapa = {}
                    for _, r in df_busca.iterrows():
                        label = f"{r['matricula']} | {r['nome']} | {r['contrato']}"
                        opcoes.append(label)
                        mapa[label] = r["matricula"]

                    escolhas = st.multiselect("Selecione os colaboradores encontrados", opcoes, key="arq_multi_busca")
                    selecionados_matriculas = [mapa[x] for x in escolhas]

            st.divider()

            if st.button("✅ Arquivar selecionados", type="primary", key="btn_arquivar"):
                if not selecionados_matriculas:
                    st.warning("Selecione pelo menos um colaborador.")
                else:
                    ts = agora_str()
                    usuario = st.session_state.usuario_logado

                    pool, conn, cur = get_conn_cursor()
                    try:
                        registros = [(mat, int(caixa_id), int(mes_id), ts) for mat in selecionados_matriculas]

                        query = """
                        INSERT INTO cartoes_ponto (matricula, caixa_id, mes_id, data_registro, status)
                        VALUES (%s,%s,%s,%s,'ARQUIVADO')
                        ON CONFLICT (matricula, mes_id)
                        DO UPDATE SET
                            caixa_id = EXCLUDED.caixa_id,
                            data_registro = EXCLUDED.data_registro,
                            status = 'ARQUIVADO',
                            data_desarquivamento = NULL,
                            usuario_desarquivou = NULL,
                            motivo_desarquivamento = NULL
                        """

                        execute_batch(cur, query, registros, page_size=500)

                        detalhes = [f"Matricula {mat} -> Caixa {caixa_id} | Mes {mes_id}" for mat in selecionados_matriculas]
                        registrar_logs_em_lote(cur, usuario, "ARQUIVAMENTO", detalhes)

                        close_conn(pool, conn, cur, commit=True)
                        st.success(f"Arquivamento concluído: {len(selecionados_matriculas)} colaborador(es).")
                        st.rerun()
                    except Exception as e:
                        close_conn(pool, conn, cur, commit=False)
                        st.error(f"Erro ao arquivar: {e}")

        # =========================================================
        # 2) DESARQUIVAR
        # =========================================================
        elif acao == "Desarquivar (retirar cartão)":
            st.caption("Desarquiva sem apagar histórico. Para rearquivar, use 'Arquivar cartões'.")

            mes_id = st.selectbox(
                "Mês",
                meses["id"].tolist(),
                format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0],
                key="desarq_mes",
            )

            df_arq = sql_df(
                """
                SELECT cp.id, cp.matricula, b.nome, b.contrato
                FROM cartoes_ponto cp
                LEFT JOIN base_colaboradores b ON b.matricula = cp.matricula
                WHERE cp.mes_id = %s AND cp.status = 'ARQUIVADO'
                ORDER BY b.nome
                """,
                params=(int(mes_id),),
            )

            if df_arq.empty:
                st.info("Nenhum cartão ARQUIVADO neste mês.")
                st.stop()

            opcoes = []
            mapa = {}
            for _, r in df_arq.iterrows():
                label = f"{r['matricula']} | {r['nome']} | {r['contrato']}"
                opcoes.append(label)
                mapa[label] = int(r["id"])

            escolhidos = st.multiselect("Selecione quem será desarquivado", opcoes, key="multi_desarq")
            motivo = st.text_input("Motivo do desarquivamento (obrigatório)", key="motivo_desarq")

            if st.button("🗑 Desarquivar selecionados", key="btn_desarq"):
                if not escolhidos:
                    st.warning("Selecione pelo menos um colaborador.")
                elif len(motivo.strip()) < 3:
                    st.warning("Informe um motivo (mínimo 3 caracteres).")
                else:
                    ts = agora_str()
                    usuario = st.session_state.usuario_logado
                    motivo_ok = motivo.strip()
                    ids = [mapa[x] for x in escolhidos]

                    pool, conn, cur = get_conn_cursor()
                    try:
                        cur.execute(
                            """
                            UPDATE cartoes_ponto
                            SET status='DESARQUIVADO',
                                data_desarquivamento=%s,
                                usuario_desarquivou=%s,
                                motivo_desarquivamento=%s
                            WHERE id = ANY(%s)
                            """,
                            (ts, usuario, motivo_ok, ids),
                        )

                        detalhes = [f"Registro {rid} | Motivo: {motivo_ok}" for rid in ids]
                        registrar_logs_em_lote(cur, usuario, "DESARQUIVAMENTO", detalhes)

                        close_conn(pool, conn, cur, commit=True)
                        st.success(f"Desarquivamento concluído: {len(ids)} registro(s).")
                        st.rerun()
                    except Exception as e:
                        close_conn(pool, conn, cur, commit=False)
                        st.error(f"Erro ao desarquivar: {e}")

        # =========================================================
        # 3) EXCLUIR CAIXA
        # =========================================================
        elif acao == "Excluir Caixa":
            st.caption("Mostra impacto. Ao confirmar, desarquiva registros e exclui a caixa.")

            mes_id = st.selectbox(
                "Mês",
                meses["id"].tolist(),
                format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0],
                key="exc_caixa_mes",
            )

            caixas_mes = sql_df(
                "SELECT * FROM caixas WHERE mes_id=%s ORDER BY numero_caixa",
                params=(int(mes_id),),
            )

            if caixas_mes.empty:
                st.info("Não há caixas neste mês.")
                st.stop()

            caixa_id = st.selectbox(
                "Selecione a caixa para excluir",
                caixas_mes["id"].tolist(),
                format_func=lambda x: (
                    f"Caixa {caixas_mes.loc[caixas_mes['id']==x,'numero_caixa'].values[0]} • "
                    f"{caixas_mes.loc[caixas_mes['id']==x,'localizacao'].values[0]}"
                ),
                key="exc_caixa_id",
            )

            impacto = sql_df(
                """
                SELECT cp.id, cp.matricula, b.nome, b.contrato
                FROM cartoes_ponto cp
                LEFT JOIN base_colaboradores b ON b.matricula = cp.matricula
                WHERE cp.caixa_id = %s AND cp.status = 'ARQUIVADO'
                ORDER BY b.nome
                """,
                params=(int(caixa_id),),
            )

            st.write("### Impacto (cartões arquivados nesta caixa)")
            st.dataframe(impacto, use_container_width=True)

            motivo = st.text_input("Motivo da exclusão (obrigatório)", key="motivo_exc_caixa")

            if st.button("❌ Confirmar exclusão da caixa", type="primary", key="btn_exc_caixa"):
                if len(motivo.strip()) < 3:
                    st.warning("Informe um motivo (mínimo 3 caracteres).")
                else:
                    pool, conn, cur = get_conn_cursor()
                    try:
                        cur.execute(
                            """
                            UPDATE cartoes_ponto
                            SET status='DESARQUIVADO',
                                data_desarquivamento=%s,
                                usuario_desarquivou=%s,
                                motivo_desarquivamento=%s
                            WHERE caixa_id=%s AND status='ARQUIVADO'
                            """,
                            (
                                agora_str(),
                                st.session_state.usuario_logado,
                                f"Exclusão da caixa {caixa_id}: {motivo.strip()}",
                                int(caixa_id),
                            ),
                        )

                        cur.execute("DELETE FROM caixas WHERE id=%s", (int(caixa_id),))

                        registrar_log(
                            cur,
                            st.session_state.usuario_logado,
                            "EXCLUSAO_CAIXA",
                            f"Caixa {caixa_id} excluída | Mes {mes_id} | Motivo: {motivo.strip()}",
                        )

                        close_conn(pool, conn, cur, commit=True)
                        st.success("Caixa excluída com sucesso (e registros desarquivados).")
                        st.rerun()
                    except Exception as e:
                        close_conn(pool, conn, cur, commit=False)
                        st.error(f"Erro ao excluir caixa: {e}")

        # =========================================================
        # 4) EXCLUIR MÊS
        # =========================================================
        elif acao == "Excluir Mês":
            st.caption("Mostra impacto. Ao confirmar, desarquiva registros, exclui caixas e exclui o mês.")

            mes_id = st.selectbox(
                "Selecione o mês para excluir",
                meses["id"].tolist(),
                format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0],
                key="exc_mes_id",
            )

            impacto_mes = sql_df(
                """
                SELECT cp.id, cp.matricula, b.nome, b.contrato, cp.caixa_id
                FROM cartoes_ponto cp
                LEFT JOIN base_colaboradores b ON b.matricula = cp.matricula
                WHERE cp.mes_id = %s AND cp.status = 'ARQUIVADO'
                ORDER BY b.nome
                """,
                params=(int(mes_id),),
            )

            qtd_caixas = sql_df(
                "SELECT COUNT(*) AS total FROM caixas WHERE mes_id=%s",
                params=(int(mes_id),),
            )["total"].iloc[0]

            st.write(f"### Caixas neste mês: **{int(qtd_caixas)}**")
            st.write("### Impacto (cartões arquivados neste mês)")
            st.dataframe(impacto_mes, use_container_width=True)

            motivo = st.text_input("Motivo da exclusão (obrigatório)", key="motivo_exc_mes")

            if st.button("❌ Confirmar exclusão do mês", type="primary", key="btn_exc_mes"):
                if len(motivo.strip()) < 3:
                    st.warning("Informe um motivo (mínimo 3 caracteres).")
                else:
                    pool, conn, cur = get_conn_cursor()
                    try:
                        cur.execute(
                            """
                            UPDATE cartoes_ponto
                            SET status='DESARQUIVADO',
                                data_desarquivamento=%s,
                                usuario_desarquivou=%s,
                                motivo_desarquivamento=%s
                            WHERE mes_id=%s AND status='ARQUIVADO'
                            """,
                            (
                                agora_str(),
                                st.session_state.usuario_logado,
                                f"Exclusão do mês {mes_id}: {motivo.strip()}",
                                int(mes_id),
                            ),
                        )

                        cur.execute("DELETE FROM caixas WHERE mes_id=%s", (int(mes_id),))
                        cur.execute("DELETE FROM meses WHERE id=%s", (int(mes_id),))

                        registrar_log(
                            cur,
                            st.session_state.usuario_logado,
                            "EXCLUSAO_MES",
                            f"Mês {mes_id} excluído | Motivo: {motivo.strip()}",
                        )

                        close_conn(pool, conn, cur, commit=True)
                        st.success("Mês excluído com sucesso (registros desarquivados e caixas removidas).")
                        st.rerun()
                    except Exception as e:
                        close_conn(pool, conn, cur, commit=False)
                        st.error(f"Erro ao excluir mês: {e}")

# =========================================================
# CONSULTAR ARQUIVAMENTOS
# =========================================================
if menu == "Consultar Arquivamentos":
    st.header("📋 Consultar Arquivamentos")

    meses = sql_df("SELECT * FROM meses ORDER BY id DESC")
    if meses.empty:
        st.warning("Nenhum mês cadastrado.")
        st.stop()

    # Começa no mês mais recente para evitar carregar todo o histórico sem necessidade.
    mes_opcoes = meses["id"].tolist() + ["Todos"]
    mes_id = st.selectbox(
        "Mês",
        mes_opcoes,
        key="cons_mes",
        format_func=lambda x: "Todos" if x == "Todos" else meses.loc[meses["id"] == x, "mes_referencia"].values[0],
    )

    contratos = ["Todos"] + listar_contratos()
    contrato_selecionado = st.selectbox("Contrato (opcional)", contratos, key="cons_contrato")

    if mes_id == "Todos":
        caixas = sql_df("SELECT * FROM caixas ORDER BY id")
    else:
        caixas = sql_df("SELECT * FROM caixas WHERE mes_id=%s ORDER BY numero_caixa", params=(int(mes_id),))

    caixa_opcoes = ["Todas"] + caixas["id"].tolist()
    caixa_selecionada = st.selectbox(
        "Caixa (opcional)",
        caixa_opcoes,
        key="cons_caixa",
        format_func=lambda x: "Todas" if x == "Todas" else f"Caixa {caixas.loc[caixas['id']==x,'numero_caixa'].values[0]}",
    )

    origem_sel = st.selectbox("Origem", ["Todas", "MANUAL", "IMPORTACAO"], key="cons_origem")
    busca = st.text_input("Buscar por nome ou matrícula", key="cons_busca")

    filtros = ["1=1"]
    params = []
    if mes_id != "Todos":
        filtros.append("cp.mes_id=%s")
        params.append(int(mes_id))
    if caixa_selecionada != "Todas":
        filtros.append("cp.caixa_id=%s")
        params.append(int(caixa_selecionada))
    if contrato_selecionado != "Todos":
        filtros.append("COALESCE(NULLIF(cp.contrato_historico,''), b.contrato)=%s")
        params.append(contrato_selecionado)
    if origem_sel != "Todas":
        filtros.append("COALESCE(cp.origem,'MANUAL')=%s")
        params.append(origem_sel)
    if busca.strip():
        termo = f"%{busca.strip()}%"
        filtros.append("(COALESCE(NULLIF(cp.nome_historico,''), b.nome) ILIKE %s OR cp.matricula ILIKE %s)")
        params.extend([termo, termo])

    where_sql = " AND ".join(filtros)

    metricas = sql_df(
        f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE cp.status='ARQUIVADO') AS arquivados,
               COUNT(*) FILTER (WHERE cp.status='DESARQUIVADO') AS desarquivados,
               COUNT(*) FILTER (WHERE COALESCE(cp.origem,'MANUAL')='IMPORTACAO') AS importados
        FROM cartoes_ponto cp
        LEFT JOIN base_colaboradores b ON cp.matricula=b.matricula
        LEFT JOIN caixas c ON cp.caixa_id=c.id
        WHERE {where_sql}
        """,
        tuple(params) if params else None,
    )

    total = int(metricas.iloc[0]["total"] if not metricas.empty else 0)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", total)
    c2.metric("Arquivados", int(metricas.iloc[0]["arquivados"] or 0))
    c3.metric("Desarquivados", int(metricas.iloc[0]["desarquivados"] or 0))
    c4.metric("Importados", int(metricas.iloc[0]["importados"] or 0))

    tamanho = st.selectbox("Registros por página", [100, 250, 500], index=1, key="cons_tamanho")
    paginas = max(1, (total + tamanho - 1) // tamanho)
    pagina = st.number_input("Página", min_value=1, max_value=paginas, value=1, step=1, key="cons_pagina")
    offset = (int(pagina) - 1) * tamanho

    query = f"""
        SELECT cp.id,
               cp.matricula,
               COALESCE(NULLIF(cp.nome_historico,''), b.nome) AS nome,
               COALESCE(NULLIF(cp.contrato_historico,''), b.contrato) AS contrato,
               m.mes_referencia,
               c.numero_caixa,
               c.localizacao,
               cp.data_registro,
               cp.status,
               COALESCE(cp.origem,'MANUAL') AS origem,
               cp.aba_origem,
               cp.arquivo_origem
        FROM cartoes_ponto cp
        LEFT JOIN base_colaboradores b ON cp.matricula = b.matricula
        LEFT JOIN caixas c ON cp.caixa_id = c.id
        LEFT JOIN meses m ON cp.mes_id = m.id
        WHERE {where_sql}
        ORDER BY cp.id DESC
        LIMIT %s OFFSET %s
    """
    df = sql_df(query, tuple(list(params) + [tamanho, offset]))

    if df.empty:
        st.info("Nenhum arquivamento encontrado com esses filtros.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("🗑 Excluir Registro (apaga da tabela)")
        st.caption("Use apenas quando realmente for necessário. A exclusão é registrada no log.")
        registro_id = st.selectbox("Selecionar ID para excluir", df["id"].tolist(), key="cons_del_id")

        if st.button("Excluir Registro", key="cons_del_btn"):
            pool, conn, cur = get_conn_cursor()
            try:
                cur.execute("DELETE FROM cartoes_ponto WHERE id=%s", (int(registro_id),))
                registrar_log(cur, st.session_state.usuario_logado, "EXCLUSAO_REGISTRO", f"Registro ID {registro_id}")
                close_conn(pool, conn, cur, commit=True)
                st.success("Registro excluído com sucesso!")
                st.rerun()
            except Exception as e:
                close_conn(pool, conn, cur, commit=False)
                st.error(f"Erro ao excluir registro: {e}")

# =========================================================
# AUDITORIA (16 a 15)
# =========================================================
if menu == "Auditoria":
    st.header("🧠 Auditoria de Cartões")

    meses = sql_df("SELECT * FROM meses ORDER BY id DESC")
    if meses.empty:
        st.warning("Cadastre meses primeiro.")
        st.stop()

    meses_ids = meses["id"].tolist()
    idx = 0
    if st.session_state.memoria.get("mes_auditoria") in meses_ids:
        idx = meses_ids.index(st.session_state.memoria.get("mes_auditoria"))

    mes_id = st.selectbox(
        "Mês para auditoria",
        meses_ids,
        index=idx,
        format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0],
        key="aud_mes",
    )
    st.session_state.memoria["mes_auditoria"] = mes_id

    mes_ref = meses.loc[meses["id"] == mes_id, "mes_referencia"].values[0].replace("-", "/")
    try:
        mes, ano = mes_ref.split("/")
        mes = int(mes)
        ano = int(ano)
    except Exception:
        st.error("Formato do mês inválido. Use 01-2026 ou 01/2026.")
        st.stop()

    if mes == 1:
        mes_anterior, ano_anterior = 12, ano - 1
    else:
        mes_anterior, ano_anterior = mes - 1, ano

    data_inicio = datetime(ano_anterior, mes_anterior, 16)
    data_fim = datetime(ano, mes, 15)

    st.info(f"Período auditado: {data_inicio.strftime('%d-%m-%Y')} até {data_fim.strftime('%d-%m-%Y')}")

    contratos = listar_contratos()
    if not contratos:
        st.warning("Sem contratos na base.")
        st.stop()

    contrato_selecionado = st.selectbox("Contrato", contratos, key="aud_contrato")

    # Carrega somente o contrato escolhido, em vez da base inteira.
    base_c = sql_df(
        """
        SELECT matricula, nome, data_admissao, data_demissao
        FROM base_colaboradores
        WHERE contrato=%s
        """,
        params=(contrato_selecionado,),
    )
    base_c["data_admissao"] = pd.to_datetime(base_c["data_admissao"], dayfirst=True, errors="coerce")
    base_c["data_demissao"] = pd.to_datetime(base_c["data_demissao"], dayfirst=True, errors="coerce")

    ativos = base_c[
        (base_c["data_admissao"] <= data_fim)
        & (base_c["data_demissao"].isna() | (base_c["data_demissao"] >= data_inicio))
    ].copy()

    total_deveriam = len(ativos)

    arquivados = sql_df(
        "SELECT matricula FROM cartoes_ponto WHERE mes_id=%s AND status='ARQUIVADO'",
        params=(int(mes_id),),
    )
    arquivados_set = set(arquivados["matricula"].astype(str))

    ativos["matricula"] = ativos["matricula"].astype(str)
    ativos["arquivado"] = ativos["matricula"].isin(arquivados_set)

    total_arquivados = int(ativos["arquivado"].sum())
    faltando = ativos[ativos["arquivado"] == False]

    c1, c2, c3 = st.columns(3)
    c1.metric("Deveriam ter cartão", total_deveriam)
    c2.metric("Arquivados", total_arquivados)
    c3.metric("Faltando", total_deveriam - total_arquivados)

    st.divider()

    if not faltando.empty:
        st.error("⚠ Colaboradores sem cartão no período:")
        st.dataframe(faltando[["matricula", "nome"]], use_container_width=True, hide_index=True)
    else:
        st.success("Todos os cartões foram arquivados nesse contrato!")

# =========================================================
# GESTÃO DE USUÁRIOS
# =========================================================
if menu == "Gestão de Usuários":
    if st.session_state.perfil != "admin":
        st.error("Acesso restrito ao administrador.")
        st.stop()

    st.header("👤 Gestão de Usuários")
    abas_u = st.tabs(["Criar Usuário", "Listar Usuários"])

    with abas_u[0]:
        novo_user = st.text_input("Usuário", key="usr_new")
        nova_senha = st.text_input("Senha", type="password", key="usr_pass")
        perfil = st.selectbox("Perfil", ["admin", "usuario"], key="usr_role")

        if st.button("Criar Usuário", key="usr_create"):
            pool, conn, cur = get_conn_cursor()
            try:
                cur.execute(
                    "INSERT INTO usuarios (username, password, perfil) VALUES (%s,%s,%s)",
                    (novo_user.strip(), nova_senha, perfil),
                )
                close_conn(pool, conn, cur, commit=True)
                st.success("Usuário criado com sucesso!")
                st.rerun()
            except psycopg2.IntegrityError:
                close_conn(pool, conn, cur, commit=False)
                st.error("Usuário já existe.")
            except Exception as e:
                close_conn(pool, conn, cur, commit=False)
                st.error(f"Erro ao criar usuário: {e}")

    with abas_u[1]:
        df_users = sql_df("SELECT id, username, perfil FROM usuarios ORDER BY id")
        st.dataframe(df_users, use_container_width=True)

        if not df_users.empty:
            user_id = st.selectbox(
                "Selecionar usuário para excluir",
                df_users["id"].tolist(),
                format_func=lambda uid: f"{df_users.loc[df_users['id']==uid,'username'].values[0]} | {df_users.loc[df_users['id']==uid,'perfil'].values[0]}",
                key="usr_del_id",
            )
            if st.button("Excluir Usuário", key="usr_del_btn"):
                alvo = df_users[df_users["id"] == user_id].iloc[0]
                if alvo["username"] == st.session_state.usuario_logado:
                    st.warning("Você não pode excluir o próprio usuário enquanto está conectado.")
                elif alvo["perfil"] == "admin" and int((df_users["perfil"] == "admin").sum()) <= 1:
                    st.warning("O sistema precisa manter pelo menos um administrador.")
                else:
                    pool, conn, cur = get_conn_cursor()
                    try:
                        cur.execute("DELETE FROM usuarios WHERE id=%s", (int(user_id),))
                        registrar_log(
                            cur, st.session_state.usuario_logado, "EXCLUSAO_USUARIO",
                            f"Usuário excluído: {alvo['username']}"
                        )
                        close_conn(pool, conn, cur, commit=True)
                        st.success("Usuário excluído!")
                        st.rerun()
                    except Exception as e:
                        close_conn(pool, conn, cur, commit=False)
                        st.error(f"Erro ao excluir usuário: {e}")
