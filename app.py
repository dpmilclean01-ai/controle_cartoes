import streamlit as st

st.set_page_config(page_title="Controle de Cartões", layout="wide")

import psycopg2
import pandas as pd
from datetime import datetime
import os
from streamlit_cookies_manager import EncryptedCookieManager
from psycopg2.extras import execute_batch

def formatar_data(valor):
    if pd.isna(valor) or valor == "":
        return None
    try:
        return pd.to_datetime(valor, dayfirst=True).strftime("%d-%m-%Y")
    except:
        return None

# -------------------------
# CONEXÃO BANCO
# -------------------------

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    st.error("DATABASE_URL não encontrada.")
    st.stop()

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# -------------------------
# COOKIES
# -------------------------

cookies = EncryptedCookieManager(
    prefix="controle_cartoes_",
    password="senha_super_secreta"
)

if not cookies.ready():
    st.stop()

# -------------------------
# CRIAÇÃO TABELAS
# -------------------------


cursor.execute("""
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
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_matricula
ON base_colaboradores (matricula)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS meses (
    id SERIAL PRIMARY KEY,
    mes_referencia TEXT UNIQUE
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS caixas (
    id SERIAL PRIMARY KEY,
    numero_caixa TEXT,
    mes_id INTEGER,
    localizacao TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS cartoes_ponto (
    id SERIAL PRIMARY KEY,
    matricula TEXT,
    caixa_id INTEGER,
    mes_id INTEGER,
    data_registro TEXT,
    UNIQUE (matricula, mes_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE,
    password TEXT,
    perfil TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    usuario TEXT,
    acao TEXT,
    detalhe TEXT,
    data TEXT
)
""")

cursor.execute("CREATE INDEX IF NOT EXISTS idx_base_matricula ON base_colaboradores(matricula)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_mes ON cartoes_ponto(mes_id)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_cartoes_matricula ON cartoes_ponto(matricula)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_caixas_mes ON caixas(mes_id)")

conn.commit()

# -------------------------
# USUÁRIO ADMIN PADRÃO
# -------------------------

cursor.execute("SELECT * FROM usuarios WHERE username = %s", ("adm",))
usuario_admin = cursor.fetchone()

if not usuario_admin:
    cursor.execute("""
        INSERT INTO usuarios (username, password, perfil)
        VALUES (%s,%s,%s)
    """, ("adm", "123", "admin"))
    conn.commit()

# -------------------------
# SESSÃO
# -------------------------

if "usuario_logado" not in st.session_state:
    st.session_state.usuario_logado = None
    st.session_state.perfil = None
if "memoria" not in st.session_state:
    st.session_state.memoria = {
        "mes_gestao": None,
        "caixa_gestao": None,
        "contrato_gestao": None,
        "mes_consulta": None,
        "mes_auditoria": None
    }
# -------------------------
# AUTO LOGIN
# -------------------------

if st.session_state.usuario_logado is None:
    user_cookie = cookies.get("usuario")

    if user_cookie:
        cursor.execute(
            "SELECT * FROM usuarios WHERE username=%s",
            (user_cookie,)
        )
        usuario = cursor.fetchone()

        if usuario:
            st.session_state.usuario_logado = usuario[1]
            st.session_state.perfil = usuario[3]

# -------------------------
# LOGIN
# -------------------------

if st.session_state.usuario_logado is None:

    st.title("🔐 Login do Sistema")

    user = st.text_input("Usuário")
    senha = st.text_input("Senha", type="password")
    manter = st.checkbox("Manter conectado")

    if st.button("Entrar"):

        cursor.execute(
            "SELECT * FROM usuarios WHERE username=%s AND password=%s",
            (user, senha)
        )
        usuario = cursor.fetchone()

        if usuario:
            st.session_state.usuario_logado = usuario[1]
            st.session_state.perfil = usuario[3]

            if manter:
                cookies["usuario"] = usuario[1]
                cookies.save()

            st.success("Login realizado!")
            st.rerun()
        else:
            st.error("Usuário ou senha inválidos.")

    st.stop()
# -------------------------
# MENU
# -------------------------

menu = st.sidebar.radio("Menu", [
    "Importar Base Excel",
    "Visualizar Base",
    "Gestão de Caixas",
    "Consultar Arquivamentos",
    "Auditoria",
    "Gestão de Usuários"
])
if st.sidebar.button("🚪 Sair"):
    st.session_state.usuario_logado = None
    st.session_state.perfil = None
    cookies["usuario"] = ""
    cookies.save()
    st.rerun()
# -------------------------
# IMPORTAÇÃO
# -------------------------

if menu == "Importar Base Excel":
    if st.session_state.perfil != "admin":
        st.error("Apenas administradores podem alterar a base.")
        st.stop()

    st.header("📊 Importar / Atualizar Base de Colaboradores")

    st.info("⚠ Datas devem estar no formato DD-MM-YYYY")

    arquivo = st.file_uploader("Envie a planilha (.xlsx)", type=["xlsx"])

    if arquivo is not None:

        try:
            df = pd.read_excel(arquivo, dtype=str)
        except:
            st.error("Erro ao ler o arquivo.")
            st.stop()

        df.columns = df.columns.str.strip().str.lower()

        obrigatorias = [
            "matricula", "nome", "contrato", "responsavel",
            "data_admissao", "data_demissao", "sit_folha"
        ]

        if not all(col in df.columns for col in obrigatorias):
            st.error("❌ A planilha não está no formato correto.")
            st.write("Colunas obrigatórias:", obrigatorias)
            st.stop()

        inseridos = 0
        atualizados = 0

        from psycopg2.extras import execute_values

        registros = []

        df["matricula"] = df["matricula"].astype(str).str.strip()
        df["data_admissao"] = df["data_admissao"].apply(formatar_data)
        df["data_demissao"] = df["data_demissao"].apply(formatar_data)
        df["ultima_atualizacao"] = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

        registros = df[[
            "matricula",
            "nome",
            "contrato",
            "responsavel",
            "data_admissao",
            "data_demissao",
            "sit_folha"
        ]].values.tolist()

        # adiciona ultima_atualizacao
        registros = [tuple(r + [df["ultima_atualizacao"][0]]) for r in registros]


        query = """
        INSERT INTO base_colaboradores
        (matricula, nome, contrato, responsavel,
        data_admissao, data_demissao, sit_folha, ultima_atualizacao)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
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

        st.success("✅ Importação concluída!")
        st.write(f"➕ Inseridos: {inseridos}")
        st.write(f"🔄 Atualizados: {atualizados}")

# -------------------------
# VISUALIZAÇÃO
# -------------------------

if menu == "Visualizar Base":

    st.header("📋 Base Atual no Sistema")

    df = pd.read_sql("SELECT * FROM base_colaboradores", conn)

    if df.empty:
        st.warning("Nenhum registro encontrado.")
    else:
        st.dataframe(df, use_container_width=True)
# -------------------------
# GESTÃO DE CAIXAS
# -------------------------

if menu == "Gestão de Caixas":

    st.header("📦 Gestão de Caixas")

    abas = st.tabs(["Criar Mês", "Criar Caixa", "Arquivar Funcionários"])

    # -------------------------
    # CRIAR MÊS
    # -------------------------
    with abas[0]:
        mes = st.text_input("Mês referência (ex: 01-2026)")
        if st.button("Salvar Mês"):
            try:
                cursor.execute("INSERT INTO meses (mes_referencia) VALUES (%s)", (mes,))
                conn.commit()
                st.success("Mês criado!")
            except:
                st.error("Mês já existe.")

    # -------------------------
    # CRIAR CAIXA
    # -------------------------
    with abas[1]:

        meses = pd.read_sql("SELECT * FROM meses", conn)

        if meses.empty:
            st.warning("Cadastre um mês primeiro.")
        else:
            mes_opcoes = ["Todos"] + meses["id"].tolist()
            mes_id = st.selectbox(
                "Mês",
                mes_opcoes,
                format_func=lambda x: "Todos" if x == "Todos"
                else meses.loc[meses["id"] == x, "mes_referencia"].values[0]
            )
            numero = st.text_input("Número da Caixa")
            local = st.text_input("Localização")

            if st.button("Criar Caixa"):
                cursor.execute("""
                    INSERT INTO caixas (numero_caixa, mes_id, localizacao)
                    VALUES (%s,%s,%s)
                """, (numero, mes_id, local))
                conn.commit()
                st.success("Caixa criada!")
    # -------------------------
    # ARQUIVAR FUNCIONÁRIOS
    # -------------------------
    with abas[2]:

        meses = pd.read_sql("SELECT * FROM meses", conn)
        base = pd.read_sql("SELECT * FROM base_colaboradores", conn)

        if meses.empty or base.empty:
            st.warning("Cadastre mês e base primeiro.")
        else:
            modo = st.radio(
                "Modo de seleção",
                ["Por contrato", "Direto por funcionário"],
                horizontal=True
            )
            if modo == "Por contrato":
                meses_ids = meses["id"].tolist()
                index_padrao = 0
                if st.session_state.memoria["mes_gestao"] in meses_ids:
                    index_padrao = meses_ids.index(st.session_state.memoria["mes_gestao"])
                mes_id = st.selectbox(
                    "Mês",
                    meses_ids,
                    index=index_padrao,
                    format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0]
    )
            caixas_mes = pd.read_sql("SELECT * FROM caixas WHERE mes_id = %s", conn, params=(mes_id,))

            if caixas_mes.empty:
                st.warning("Nenhuma caixa criada para este mês.")
            else:
                caixas_ids = caixas_mes["id"].tolist()
                index_caixa = 0
                if st.session_state.memoria["caixa_gestao"] in caixas_ids:
                    index_caixa = caixas_ids.index(st.session_state.memoria["caixa_gestao"])
                caixa_id = st.selectbox(
                    "Caixa",
                    caixas_ids,
                    index=index_caixa,
                    format_func=lambda x: f"Caixa {caixas_mes.loc[caixas_mes['id']==x,'numero_caixa'].values[0]}"
                )
                st.session_state.memoria["caixa_gestao"] = caixa_id

                if modo == "Por contrato":
                    contratos_lista = sorted(base["contrato"].dropna().unique().tolist())
                    contrato = st.selectbox("Contrato", contratos_lista)

                    funcionarios = base[base["contrato"] == contrato].sort_values(by="matricula")

                    selecionados = st.multiselect(
                        "Selecionar funcionários arquivados",
                        funcionarios["matricula"].tolist(),
                        format_func=lambda x: f"{x} - {funcionarios.loc[funcionarios['matricula']==x,'nome'].values[0]}"
                    )
                else:
                    busca = st.text_input("Buscar por nome ou matrícula (mín. 3 caracteres)")

                    if not busca or len(busca.strip()) < 3:
                        st.info("Digite pelo menos 3 caracteres (nome OU matrícula) para carregar a lista.")
                        selecionados = []
                    else:
                        b = busca.strip()

                        base_filtrada = base[
                            base["nome"].fillna("").str.contains(b, case=False, na=False) |
                            base["matricula"].fillna("").str.contains(b, case=False, na=False)
                        ].copy()

                        base_filtrada = base_filtrada.sort_values(by=["contrato", "nome", "matricula"])

                        limite = 300
                        if len(base_filtrada) > limite:
                            st.warning(f"Muitos resultados ({len(base_filtrada)}). Mostrando apenas os primeiros {limite}. Refine a busca.")
                            base_filtrada = base_filtrada.head(limite)

                        base_filtrada["label"] = (
                            base_filtrada["matricula"].fillna("") + " | " +
                            base_filtrada["nome"].fillna("") + " | " +
                            base_filtrada["contrato"].fillna("")
                        )

                        opcoes = dict(zip(base_filtrada["label"], base_filtrada["matricula"]))

                        escolhidos_label = st.multiselect(
                            "Selecione os colaboradores encontrados",
                            options=list(opcoes.keys())
                        )

                        selecionados = [opcoes[lbl] for lbl in escolhidos_label]

                    # evita travar se sua base for gigante (ex: 20k+)
                    base_filtrada = base_filtrada.head(500)

                    selecionados = st.multiselect(
                        "Selecionar funcionários arquivados",
                        base_filtrada["matricula"].tolist(),
                        format_func=lambda x: (
                            f"{x} - {base.loc[base['matricula']==x,'nome'].values[0]} "
                            f"(Contrato: {base.loc[base['matricula']==x,'contrato'].values[0]})"
                        )
                    )
                if st.button("Salvar Arquivamento"):
                    registros = []

                    for mat in selecionados:
                        registros.append((
                            mat,
                            caixa_id,
                            mes_id,
                            datetime.now().strftime("%d-%m-%Y %H:%M:%S")
                        ))

                    query = """
                    INSERT INTO cartoes_ponto
                    (matricula, caixa_id, mes_id, data_registro)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (matricula, mes_id) DO NOTHING
                    """

                    execute_batch(cursor, query, registros, page_size=500)
                    conn.commit()

                    st.success("Processamento concluído!")

# -------------------------
# CONSULTAR ARQUIVAMENTOS
# -------------------------

if menu == "Consultar Arquivamentos":

    st.header("📋 Consultar / Editar Arquivamentos")

    meses = pd.read_sql("SELECT * FROM meses", conn)

    if meses.empty:
        st.warning("Nenhum mês cadastrado.")
    else:
        # -------------------------
        # FILTRO MÊS (OBRIGATÓRIO)
        # -------------------------
        mes_opcoes = ["Todos"] + meses["id"].tolist()

        mes_id = st.selectbox(
            "Mês",
            mes_opcoes,
            format_func=lambda x: "Todos" if x == "Todos"
            else meses.loc[meses["id"] == x, "mes_referencia"].values[0]
        )

        st.session_state.memoria["mes_consulta"] = mes_id
        # -------------------------
        # FILTRO CAIXA (OPCIONAL)
        # -------------------------
        caixas = pd.read_sql("SELECT * FROM caixas WHERE mes_id = %s", conn, params=(mes_id,))
        
        caixa_opcoes = ["Todas"] + caixas["id"].tolist()

        caixa_selecionada = st.selectbox(
            "Caixa (opcional)",
            caixa_opcoes,
            format_func=lambda x: "Todas" if x == "Todas"
            else f"Caixa {caixas.loc[caixas['id']==x,'numero_caixa'].values[0]}"
        )

        # -------------------------
        # FILTRO CONTRATO (OPCIONAL)
        # -------------------------
        base = pd.read_sql("SELECT * FROM base_colaboradores", conn)
        contratos = ["Todos"] + sorted(base["contrato"].dropna().unique().tolist())

        contrato_selecionado = st.selectbox("Contrato (opcional)", contratos)

        # -------------------------
        # BUSCA POR NOME / MATRÍCULA
        # -------------------------
        busca = st.text_input("Buscar por nome ou matrícula")

        # -------------------------
        # QUERY BASE
        # -------------------------
        query = """
        SELECT cp.id, cp.matricula, b.nome, b.contrato,
            c.numero_caixa, c.localizacao, cp.data_registro
        FROM cartoes_ponto cp
        LEFT JOIN base_colaboradores b ON cp.matricula = b.matricula
        LEFT JOIN caixas c ON cp.caixa_id = c.id
        WHERE 1=1
        """

        params = []

        if mes_id != "Todos":
            query += " AND cp.mes_id = %s"
            params.append(mes_id)


        # Filtro Caixa
        if caixa_selecionada != "Todas":
            query += " AND cp.caixa_id = %s"
            params.append(caixa_selecionada)

        # Filtro Contrato
        if contrato_selecionado != "Todos":
            query += " AND b.contrato = %s"
            params.append(contrato_selecionado)

        df = pd.read_sql(query, conn, params=params)

        # Filtro Busca
        if busca:
            df = df[
                df["nome"].str.contains(busca, case=False, na=False) |
                df["matricula"].str.contains(busca, case=False, na=False)
            ]

        if df.empty:
            st.info("Nenhum arquivamento encontrado com esses filtros.")
        else:
            st.dataframe(df, use_container_width=True)

            st.divider()
            st.subheader("🗑 Excluir Arquivamento")

            registro_id = st.selectbox("Selecionar ID para excluir", df["id"])

            if st.button("Excluir Registro"):
                cursor.execute("DELETE FROM cartoes_ponto WHERE id = %s", (registro_id,))
                conn.commit()
                st.success("Registro excluído com sucesso!")
                cursor.execute("""
                    INSERT INTO logs (usuario, acao, detalhe, data)
                    VALUES (%s,%s,%s,%s)
                """, (
                    st.session_state.usuario_logado,
                    "EXCLUSAO",
                    f"Registro ID {registro_id}",
                    datetime.now().strftime("%d-%m-%Y %H:%M:%S")
                ))
                conn.commit()

# -------------------------
# MOTOR DE AUDITORIA V1
# -------------------------

if menu == "Auditoria":

    st.header("🧠 Auditoria de Cartões")

    meses = pd.read_sql("SELECT * FROM meses", conn)
    base = pd.read_sql("SELECT * FROM base_colaboradores", conn)

    if meses.empty:
        st.warning("Cadastre meses primeiro.")
        st.stop()

    meses_ids = meses["id"].tolist()

    index_padrao = 0
    if st.session_state.memoria["mes_auditoria"] in meses_ids:
        index_padrao = meses_ids.index(st.session_state.memoria["mes_auditoria"])

    mes_id = st.selectbox(
        "Mês para auditoria",
        meses_ids,
        index=index_padrao,
        format_func=lambda x: meses.loc[meses["id"] == x, "mes_referencia"].values[0]
    )

    st.session_state.memoria["mes_auditoria"] = mes_id


    mes_ref = meses.loc[meses["id"] == mes_id, "mes_referencia"].values[0]

    # -------------------------
    # CALCULAR PERÍODO 16 A 15
    # -------------------------

    mes, ano = mes_ref.split("/")
    mes = int(mes)
    ano = int(ano)

    if mes == 1:
        mes_anterior = 12
        ano_anterior = ano - 1
    else:
        mes_anterior = mes - 1
        ano_anterior = ano

    data_inicio = datetime(ano_anterior, mes_anterior, 16)
    data_fim = datetime(ano, mes, 15)

    st.info(f"Período auditado: {data_inicio.strftime('%d-%m-%Y')} até {data_fim.strftime('%d-%m-%Y')}")

    # -------------------------
    # FILTRO CONTRATO
    # -------------------------

    contratos = sorted(base["contrato"].dropna().unique().tolist())
    contrato_selecionado = st.selectbox("Contrato", contratos)

    base_contrato = base[base["contrato"] == contrato_selecionado].copy()

    # Converter datas
    base_contrato["data_admissao"] = pd.to_datetime(base_contrato["data_admissao"], dayfirst=True, errors="coerce")
    base_contrato["data_demissao"] = pd.to_datetime(base_contrato["data_demissao"], dayfirst=True, errors="coerce")

    # -------------------------
    # REGRA DE ATIVIDADE NO PERÍODO
    # -------------------------

    ativos_periodo = base_contrato[
        (base_contrato["data_admissao"] <= data_fim) &
        (
            base_contrato["data_demissao"].isna() |
            (base_contrato["data_demissao"] >= data_inicio)
        )
    ]

    total_deveriam = len(ativos_periodo)

    # -------------------------
    # VERIFICAR ARQUIVADOS
    # -------------------------

    arquivados = pd.read_sql("""
        SELECT matricula FROM cartoes_ponto
        WHERE mes_id = %s
    """, conn, params=(mes_id,))

    arquivados_set = set(arquivados["matricula"].astype(str))

    ativos_periodo["matricula"] = ativos_periodo["matricula"].astype(str)

    ativos_periodo["arquivado"] = ativos_periodo["matricula"].isin(arquivados_set)

    total_arquivados = ativos_periodo["arquivado"].sum()

    faltando = ativos_periodo[ativos_periodo["arquivado"] == False]

    # -------------------------
    # RESULTADOS
    # -------------------------

    col1, col2, col3 = st.columns(3)

    col1.metric("Deveriam ter cartão", total_deveriam)
    col2.metric("Arquivados", total_arquivados)
    col3.metric("Faltando", total_deveriam - total_arquivados)

    st.divider()

    if not faltando.empty:
        st.error("⚠ Colaboradores sem cartão no período:")
        st.dataframe(faltando[["matricula", "nome"]], use_container_width=True)
    else:
        st.success("Todos os cartões foram arquivados nesse contrato!")

    st.warning(
        "⚠ Esta auditoria considera:\n"
        "- Contrato atual do colaborador\n"
        "- Não considera histórico de transferências\n"
        "- Não considera período exato de férias ou afastamento\n"
        "Verifique manualmente casos específicos."
    )
    
# -------------------------
# GESTÃO DE USUÁRIOS
# -------------------------

if menu == "Gestão de Usuários":

    if st.session_state.perfil != "admin":
        st.error("Acesso restrito ao administrador.")
        st.stop()

    st.header("👤 Gestão de Usuários")

    abas = st.tabs(["Criar Usuário", "Listar Usuários"])

    # -------------------------
    # CRIAR
    # -------------------------
    with abas[0]:

        
        novo_user = st.text_input("Usuário")
        nova_senha = st.text_input("Senha", type="password")
        perfil = st.selectbox("Perfil", ["admin", "usuario"])

        if st.button("Criar Usuário"):
            try:
                cursor.execute("""
                    INSERT INTO usuarios (username, password, perfil)
                    VALUES (%s,%s,%s)
                """, (novo_user, nova_senha, perfil))
                conn.commit()
                st.success("Usuário criado com sucesso!")
            except psycopg2.IntegrityError:
                conn.rollback()
                st.error("Usuário já existe.")
    # -------------------------
    # LISTAR
    # -------------------------
    with abas[1]:

        df_users = pd.read_sql("SELECT id, username, perfil FROM usuarios", conn)

        st.dataframe(df_users, use_container_width=True)

        user_id = st.selectbox("Selecionar usuário para excluir", df_users["id"])

        if st.button("Excluir Usuário"):
            cursor.execute("DELETE FROM usuarios WHERE id = %s", (user_id,))
            conn.commit()
            st.success("Usuário excluído!")
