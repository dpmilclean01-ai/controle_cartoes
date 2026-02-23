import os
from datetime import datetime

import pandas as pd
import psycopg2
import streamlit as st
from psycopg2.extras import execute_batch, execute_values
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
        return pd.to_datetime(v, dayfirst=True, errors="coerce").strftime("%d-%m-%Y")
    except Exception:
        return None


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
    registros = [(usuario, acao, d, agora_str()) for d in detalhes]
    execute_values(
        cur,
        "INSERT INTO logs (usuario, acao, detalhe, data) VALUES %s",
        registros,
        page_size=1000
    )


def buscar_colaboradores(conn, termo, limite=150):
    termo = (termo or "").strip()
    if len(termo) < 2:
        return pd.DataFrame(columns=["matricula", "nome", "contrato"])

    return pd.read_sql(
        """
        SELECT matricula, nome, contrato
        FROM base_colaboradores
        WHERE matricula ILIKE %s OR nome ILIKE %s
        ORDER BY nome
        LIMIT %s
        """,
        conn,
        params=(f"%{termo}%", f"%{termo}%", limite),
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
# BANCO
# =========================================================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    st.error("DATABASE_URL não encontrada (configure nas variáveis do Streamlit Cloud).")
    st.stop()

from psycopg2.pool import ThreadedConnectionPool

@st.cache_resource
def get_pool():
    return ThreadedConnectionPool(
        minconn=1,
        maxconn=10,  # se tiver muitos usuários simultâneos, suba para 20
        dsn=DATABASE_URL
    )

def get_conn_cursor():
    pool = get_pool()
    conn = pool.getconn()
    conn.autocommit = False
    cur = conn.cursor()
    return pool, conn, cur

def close_conn(pool, conn, cur, commit=True):
    try:
        if commit:
            conn.commit()
        else:
            conn.rollback()
    finally:
        try:
            cur.close()
        except Exception:
            pass
        pool.putconn(conn)
# =========================================================
# COOKIES
# =========================================================
cookies = EncryptedCookieManager(prefix="controle_cartoes_", password="senha_super_secreta")
if not cookies.ready():
    st.stop()

# =========================================================
# TABELAS / MIGRAÇÕES
# =========================================================
pool, conn, cursor = get_conn_cursor()
try:
    cursor.execute(
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

    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS meses (
        id SERIAL PRIMARY KEY,
        mes_referencia TEXT UNIQUE
    )
    """
    )

    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS caixas (
        id SERIAL PRIMARY KEY,
        numero_caixa TEXT,
        mes_id INTEGER,
        localizacao TEXT
    )
    """
    )

    cursor.execute(
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

    # Opção B (histórico)
    cursor.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ARQUIVADO'")
    cursor.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS data_desarquivamento TEXT")
    cursor.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS usuario_desarquivou TEXT")
    cursor.execute("ALTER TABLE cartoes_ponto ADD COLUMN IF NOT EXISTS motivo_desarquivamento TEXT")

    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT,
        perfil TEXT
    )
    """
    )

    cursor.execute(
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

    # Índices
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_base_matricula ON base_colaboradores(matricula)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_mes ON cartoes_ponto(mes_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_matricula ON cartoes_ponto(matricula)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_caixas_mes ON caixas(mes_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_mes_status ON cartoes_ponto(mes_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_caixa_status ON cartoes_ponto(caixa_id, status)")
    conn.commit()
    
    close_conn(pool, conn, cursor, commit=True)
except Exception as e:
    close_conn(pool, conn, cursor, commit=False)
    st.error(f"Erro nas migrações/tabelas: {e}")
    st.stop()

# =========================================================
# ADMIN PADRÃO
# =========================================================
pool, conn, cursor = get_conn_cursor()
try:
    cursor.execute("SELECT 1 FROM usuarios WHERE username=%s", ("adm",))
    existe = cursor.fetchone()

    if not existe:
        cursor.execute(
            "INSERT INTO usuarios (username, password, perfil) VALUES (%s,%s,%s)",
            ("adm", "123", "admin"),
        )

    close_conn(pool, conn, cursor, commit=True)
except Exception as e:
    close_conn(pool, conn, cursor, commit=False)
    st.error(f"Erro ao garantir admin padrão: {e}")
    st.stop()

# =========================================================
# AUTO LOGIN
# =========================================================
if st.session_state.usuario_logado is None:
    try:
        user_cookie = cookies.get("usuario")
    except Exception:
        user_cookie = None

    if user_cookie:
        pool, conn, cursor = get_conn_cursor()
        try:
            cursor.execute("SELECT username, perfil FROM usuarios WHERE username=%s", (user_cookie,))
            usuario = cursor.fetchone()
            close_conn(pool, conn, cursor, commit=True)

            if usuario:
                st.session_state.usuario_logado = usuario[0]
                st.session_state.perfil = usuario[1]
        except Exception as e:
            close_conn(pool, conn, cursor, commit=False)
            # aqui não precisa parar o app; só ignora o cookie se deu erro
            st.warning(f"Falha no auto login (cookie ignorado): {e}")

# =========================================================
# LOGIN
# =========================================================
if st.session_state.usuario_logado is None:
    st.title("🔐 Login do Sistema")

    user = st.text_input("Usuário", key="login_user")
    senha = st.text_input("Senha", type="password", key="login_pass")
    manter = st.checkbox("Manter conectado", key="login_keep")

    if st.button("Entrar", key="login_btn"):
        pool, conn, cursor = get_conn_cursor()
        try:
            cursor.execute(
                "SELECT username, perfil FROM usuarios WHERE username=%s AND password=%s",
                (user, senha),
            )
            usuario = cursor.fetchone()
            close_conn(pool, conn, cursor, commit=True)

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
            close_conn(pool, conn, cursor, commit=False)
            st.error(f"Erro no login: {e}")

    st.stop()
# =========================================================
# MENU
# =========================================================
menu = st.sidebar.radio(
    "Menu",
    [
        "Importar Base Excel",
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

        df["matricula"] = df["matricula"].astype(str).str.strip()
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

        execute_values(cursor, query, registros, page_size=2000)
        conn.commit()

        st.success(f"✅ Importação concluída: {len(registros)} registro(s) processado(s).")

# =========================================================
# VISUALIZAR BASE
# =========================================================
if menu == "Visualizar Base":
    st.header("📋 Base Atual no Sistema")
    df = pd.read_sql("SELECT * FROM base_colaboradores ORDER BY id DESC", conn)
    if df.empty:
        st.warning("Nenhum registro encontrado.")
    else:
        st.dataframe(df, use_container_width=True)

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
        mes = st.text_input("Mês referência (ex: 01-2026)", key="criar_mes_txt")
        if st.button("Salvar Mês", key="criar_mes_btn"):
            try:
                cursor.execute("INSERT INTO meses (mes_referencia) VALUES (%s)", (mes.strip(),))
                conn.commit()
                st.success("Mês criado!")
            except Exception:
                conn.rollback()
                st.error("Mês já existe ou valor inválido.")

    # -------------------------
    # CRIAR CAIXA
    # -------------------------
    with abas[1]:
        meses = pd.read_sql("SELECT * FROM meses ORDER BY id DESC", conn)
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
                    cursor.execute(
                        "INSERT INTO caixas (numero_caixa, mes_id, localizacao) VALUES (%s,%s,%s)",
                        (numero.strip(), int(mes_id), (local or "").strip()),
                    )
                    conn.commit()
                    st.success("Caixa criada!")

    # -------------------------
    # OPERAÇÕES (Arquivar/Desarquivar/Excluir)
    # -------------------------
    with abas[2]:
        st.subheader("📌 Operações")

        meses = pd.read_sql("SELECT * FROM meses ORDER BY id DESC", conn)
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

            caixas_mes = pd.read_sql(
                "SELECT * FROM caixas WHERE mes_id=%s ORDER BY numero_caixa",
                conn,
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
                format_func=lambda x: f"Caixa {caixas_mes.loc[caixas_mes['id']==x,'numero_caixa'].values[0]} • {caixas_mes.loc[caixas_mes['id']==x,'localizacao'].values[0]}",
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

            base = pd.read_sql("SELECT matricula, nome, contrato FROM base_colaboradores", conn)
            if base.empty:
                st.warning("Base de colaboradores vazia. Importe a base primeiro.")
                st.stop()

            selecionados_matriculas = []

            if modo == "Por contrato":
                contratos_lista = sorted(base["contrato"].dropna().unique().tolist())
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

                funcionarios = base[base["contrato"] == contrato].sort_values(by="matricula").copy()

                selecionados_matriculas = st.multiselect(
                    "Selecione os funcionários",
                    funcionarios["matricula"].tolist(),
                    format_func=lambda m: f"{m} | {funcionarios.loc[funcionarios['matricula']==m,'nome'].values[0]} | {contrato}",
                    key="arq_multi_contrato",
                )

            else:
                termo = st.text_input(
                    "Digite parte do nome ou matrícula (mínimo 2 caracteres)",
                    key="busca_func",
                )
                df_busca = buscar_colaboradores(conn, termo)

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

                    pool, conn, cursor = get_conn_cursor()
                    try:
                        # 1) grava/atualiza cartões em lote
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

                        execute_batch(cursor, query, registros, page_size=500)

                        # 2) logs em lote (1 insert gigante ao invés de for)
                        detalhes = [f"Matricula {mat} -> Caixa {caixa_id} | Mes {mes_id}" for mat in selecionados_matriculas]
                        registrar_logs_em_lote(cursor, usuario, "ARQUIVAMENTO", detalhes)

                        close_conn(pool, conn, cursor, commit=True)

                        st.success(f"Arquivamento concluído: {len(selecionados_matriculas)} colaborador(es).")

                    except Exception as e:
                        close_conn(pool, conn, cursor, commit=False)
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

            df_arq = pd.read_sql(
                """
                SELECT cp.id, cp.matricula, b.nome, b.contrato
                FROM cartoes_ponto cp
                LEFT JOIN base_colaboradores b ON b.matricula = cp.matricula
                WHERE cp.mes_id = %s AND cp.status = 'ARQUIVADO'
                ORDER BY b.nome
                """,
                conn,
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

                    pool, conn, cursor = get_conn_cursor()
                    try:
                        # 1) desarquiva em lote
                        cursor.execute(
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

                        # 2) logs em lote
                        detalhes = [f"Registro {rid} | Motivo: {motivo_ok}" for rid in ids]
                        registrar_logs_em_lote(cursor, usuario, "DESARQUIVAMENTO", detalhes)

                        close_conn(pool, conn, cursor, commit=True)

                        st.success(f"Desarquivamento concluído: {len(ids)} registro(s).")

                    except Exception as e:
                        close_conn(pool, conn, cursor, commit=False)
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

            caixas_mes = pd.read_sql(
                "SELECT * FROM caixas WHERE mes_id=%s ORDER BY numero_caixa",
                conn,
                params=(int(mes_id),),
            )

            if caixas_mes.empty:
                st.info("Não há caixas neste mês.")
                st.stop()

            caixa_id = st.selectbox(
                "Selecione a caixa para excluir",
                caixas_mes["id"].tolist(),
                format_func=lambda x: f"Caixa {caixas_mes.loc[caixas_mes['id']==x,'numero_caixa'].values[0]} • {caixas_mes.loc[caixas_mes['id']==x,'localizacao'].values[0]}",
                key="exc_caixa_id",
            )

            impacto = pd.read_sql(
                """
                SELECT cp.id, cp.matricula, b.nome, b.contrato
                FROM cartoes_ponto cp
                LEFT JOIN base_colaboradores b ON b.matricula = cp.matricula
                WHERE cp.caixa_id = %s AND cp.status = 'ARQUIVADO'
                ORDER BY b.nome
                """,
                conn,
                params=(int(caixa_id),),
            )

            st.write("### Impacto (cartões arquivados nesta caixa)")
            st.dataframe(impacto, use_container_width=True)

            motivo = st.text_input("Motivo da exclusão (obrigatório)", key="motivo_exc_caixa")

            if st.button("❌ Confirmar exclusão da caixa", type="primary", key="btn_exc_caixa"):
                if len(motivo.strip()) < 3:
                    st.warning("Informe um motivo (mínimo 3 caracteres).")
                else:
                    cursor.execute(
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

                    cursor.execute("DELETE FROM caixas WHERE id=%s", (int(caixa_id),))

                    registrar_log(
                        cursor,
                        st.session_state.usuario_logado,
                        "EXCLUSAO_CAIXA",
                        f"Caixa {caixa_id} excluída | Mes {mes_id} | Motivo: {motivo.strip()}",
                    )

                    conn.commit()
                    st.success("Caixa excluída com sucesso (e registros desarquivados).")

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

            impacto_mes = pd.read_sql(
                """
                SELECT cp.id, cp.matricula, b.nome, b.contrato, cp.caixa_id
                FROM cartoes_ponto cp
                LEFT JOIN base_colaboradores b ON b.matricula = cp.matricula
                WHERE cp.mes_id = %s AND cp.status = 'ARQUIVADO'
                ORDER BY b.nome
                """,
                conn,
                params=(int(mes_id),),
            )

            qtd_caixas = pd.read_sql(
                "SELECT COUNT(*) AS total FROM caixas WHERE mes_id=%s",
                conn,
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
                    cursor.execute(
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

                    cursor.execute("DELETE FROM caixas WHERE mes_id=%s", (int(mes_id),))
                    cursor.execute("DELETE FROM meses WHERE id=%s", (int(mes_id),))

                    registrar_log(
                        cursor,
                        st.session_state.usuario_logado,
                        "EXCLUSAO_MES",
                        f"Mês {mes_id} excluído | Motivo: {motivo.strip()}",
                    )

                    conn.commit()
                    st.success("Mês excluído com sucesso (registros desarquivados e caixas removidas).")

# =========================================================
# CONSULTAR ARQUIVAMENTOS
# =========================================================
if menu == "Consultar Arquivamentos":
    st.header("📋 Consultar Arquivamentos")

    meses = pd.read_sql("SELECT * FROM meses ORDER BY id DESC", conn)
    if meses.empty:
        st.warning("Nenhum mês cadastrado.")
        st.stop()

    mes_opcoes = ["Todos"] + meses["id"].tolist()
    mes_id = st.selectbox(
        "Mês",
        mes_opcoes,
        key="cons_mes",
        format_func=lambda x: "Todos"
        if x == "Todos"
        else meses.loc[meses["id"] == x, "mes_referencia"].values[0],
    )

    pool, conn, cur = get_conn_cursor()
try:
    base = pd.read_sql("SELECT matricula, nome, contrato FROM base_colaboradores", conn)
    close_conn(pool, conn, cur, commit=True)
except Exception as e:
    close_conn(pool, conn, cur, commit=False)
    st.error(f"Erro ao carregar base: {e}")
    st.stop()
    contratos = ["Todos"] + sorted(base["contrato"].dropna().unique().tolist())
    contrato_selecionado = st.selectbox("Contrato (opcional)", contratos, key="cons_contrato")

    if mes_id == "Todos":
        caixas = pd.read_sql("SELECT * FROM caixas ORDER BY id", conn)
    else:
        caixas = pd.read_sql("SELECT * FROM caixas WHERE mes_id=%s ORDER BY id", conn, params=(int(mes_id),))

    caixa_opcoes = ["Todas"] + caixas["id"].tolist()
    caixa_selecionada = st.selectbox(
        "Caixa (opcional)",
        caixa_opcoes,
        key="cons_caixa",
        format_func=lambda x: "Todas"
        if x == "Todas"
        else f"Caixa {caixas.loc[caixas['id']==x,'numero_caixa'].values[0]}",
    )

    busca = st.text_input("Buscar por nome ou matrícula", key="cons_busca")

    query = """
        SELECT cp.id, cp.matricula, b.nome, b.contrato,
               c.numero_caixa, c.localizacao, cp.data_registro, cp.status
        FROM cartoes_ponto cp
        LEFT JOIN base_colaboradores b ON cp.matricula = b.matricula
        LEFT JOIN caixas c ON cp.caixa_id = c.id
        WHERE 1=1
    """
    params = []

    if mes_id != "Todos":
        query += " AND cp.mes_id=%s"
        params.append(int(mes_id))

    if caixa_selecionada != "Todas":
        query += " AND cp.caixa_id=%s"
        params.append(int(caixa_selecionada))

    if contrato_selecionado != "Todos":
        query += " AND b.contrato=%s"
        params.append(contrato_selecionado)

    df = pd.read_sql(query, conn, params=params)

    if busca:
        df = df[
            df["nome"].str.contains(busca, case=False, na=False)
            | df["matricula"].astype(str).str.contains(busca, case=False, na=False)
        ]

    if df.empty:
        st.info("Nenhum arquivamento encontrado com esses filtros.")
    else:
        st.dataframe(df, use_container_width=True)

        st.divider()
        st.subheader("🗑 Excluir Registro (apaga da tabela)")
        registro_id = st.selectbox("Selecionar ID para excluir", df["id"].tolist(), key="cons_del_id")

        if st.button("Excluir Registro", key="cons_del_btn"):
            cursor.execute("DELETE FROM cartoes_ponto WHERE id=%s", (int(registro_id),))
            registrar_log(cursor, st.session_state.usuario_logado, "EXCLUSAO_REGISTRO", f"Registro ID {registro_id}")
            conn.commit()
            st.success("Registro excluído com sucesso!")
            st.rerun()

# =========================================================
# AUDITORIA (simples)
# =========================================================
if menu == "Auditoria":
    st.header("🧠 Auditoria de Cartões")

    meses = pd.read_sql("SELECT * FROM meses ORDER BY id DESC", conn)
    base = pd.read_sql("SELECT * FROM base_colaboradores", conn)

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

    mes_ref = meses.loc[meses["id"] == mes_id, "mes_referencia"].values[0]

    # aceita "01-2026" ou "01/2026"
    mes_ref = mes_ref.replace("-", "/")
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

    contratos = sorted(base["contrato"].dropna().unique().tolist())
    if not contratos:
        st.warning("Sem contratos na base.")
        st.stop()

    contrato_selecionado = st.selectbox("Contrato", contratos, key="aud_contrato")

    base_c = base[base["contrato"] == contrato_selecionado].copy()
    base_c["data_admissao"] = pd.to_datetime(base_c["data_admissao"], dayfirst=True, errors="coerce")
    base_c["data_demissao"] = pd.to_datetime(base_c["data_demissao"], dayfirst=True, errors="coerce")

    ativos = base_c[
        (base_c["data_admissao"] <= data_fim)
        & (base_c["data_demissao"].isna() | (base_c["data_demissao"] >= data_inicio))
    ].copy()

    total_deveriam = len(ativos)

    arquivados = pd.read_sql(
        "SELECT matricula FROM cartoes_ponto WHERE mes_id=%s AND status='ARQUIVADO'",
        conn,
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
        st.dataframe(faltando[["matricula", "nome"]], use_container_width=True)
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
            try:
                cursor.execute(
                    "INSERT INTO usuarios (username, password, perfil) VALUES (%s,%s,%s)",
                    (novo_user.strip(), nova_senha, perfil),
                )
                conn.commit()
                st.success("Usuário criado com sucesso!")
            except psycopg2.IntegrityError:
                conn.rollback()
                st.error("Usuário já existe.")
            except Exception:
                conn.rollback()
                st.error("Erro ao criar usuário.")

    with abas_u[1]:
        df_users = pd.read_sql("SELECT id, username, perfil FROM usuarios ORDER BY id", conn)
        st.dataframe(df_users, use_container_width=True)

        user_id = st.selectbox("Selecionar usuário para excluir", df_users["id"].tolist(), key="usr_del_id")
        if st.button("Excluir Usuário", key="usr_del_btn"):
            cursor.execute("DELETE FROM usuarios WHERE id=%s", (int(user_id),))
            conn.commit()
            st.success("Usuário excluído!")
            st.rerun()
