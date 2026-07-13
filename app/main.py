from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_, nullslast, inspect, text
from typing import List, Optional
from pydantic import BaseModel
from . import models, schemas, auth
from .database import engine, get_db
import os, uuid, requests as _http, secrets, smtplib, datetime, threading, time as _time
from email.mime.text import MIMEText

# En Railway el disco es efímero: montar un Volume y apuntar UPLOAD_DIR ahí
# (p. ej. UPLOAD_DIR=/data/uploads) para que las fotos sobrevivan a los redeploys.
GREMIOS_VALIDOS = [
    "fontanero", "electricista", "cerrajero", "pintor", "carpintero",
    "albanil", "climatizacion", "jardinero", "limpieza", "mudanzas",
    "montador", "cristalero",
]

def _norm_email(email: str) -> str:
    """Un email dado por el usuario (registro, login, reset...) se compara siempre
    en minúsculas y sin espacios, para que un teclado que capitaliza la primera
    letra o un espacio de más no rompa el login/registro."""
    return (email or "").strip().lower()

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

models.Base.metadata.create_all(bind=engine)

def _migrar_columnas_faltantes():
    """create_all() no altera tablas ya existentes: agrega columnas nuevas del modelo
    que todavía no existan en la base de datos real (necesario tras cada cambio de esquema)."""
    inspector = inspect(engine)
    tablas = inspector.get_table_names()
    por_tabla = {
        "fontaneros": {
            "latitud": "FLOAT",
            "longitud": "FLOAT",
            "ubicacion_actualizada": "TIMESTAMP",
            "stripe_account_id": "VARCHAR",
            "comision_checkout_session": "VARCHAR",
        },
        "usuarios": {
            "terminos_aceptados": "BOOLEAN DEFAULT FALSE",
            "email_verificado": "BOOLEAN DEFAULT FALSE",
            "telefono_verificado": "BOOLEAN DEFAULT FALSE",
            "bloqueado": "BOOLEAN DEFAULT FALSE",
        },
        "servicios": {
            "comision_liquidada": "BOOLEAN DEFAULT TRUE",
        },
        "citas": {
            "recordatorio_24h": "BOOLEAN DEFAULT FALSE",
            "recordatorio_1h": "BOOLEAN DEFAULT FALSE",
        },
    }
    with engine.begin() as conn:
        for tabla, columnas_nuevas in por_tabla.items():
            if tabla not in tablas:
                continue
            existentes = {c["name"] for c in inspector.get_columns(tabla)}
            faltantes = {n: t for n, t in columnas_nuevas.items() if n not in existentes}
            for nombre, tipo in faltantes.items():
                conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}"))

_migrar_columnas_faltantes()

def _limpiar_valoraciones_ficticias():
    """Corrige fontaneros que aún tienen el 5.0 de relleno de antes de tener reseñas reales."""
    from .database import SessionLocal
    db = SessionLocal()
    try:
        sin_resenas = db.query(models.Fontanero).filter(
            models.Fontanero.valoracion == 5.0,
            ~models.Fontanero.id.in_(db.query(models.Resena.fontanero_id).distinct()),
        ).all()
        for f in sin_resenas:
            f.valoracion = None
        if sin_resenas:
            db.commit()
    finally:
        db.close()

_limpiar_valoraciones_ficticias()

def _normalizar_emails_existentes():
    """Los emails guardados antes de este cambio pueden tener mayúsculas o espacios;
    los pasa a minúsculas/sin espacios para que coincidan con las nuevas búsquedas
    normalizadas. Si dos cuentas distintas normalizan al mismo email (caso raro:
    'Ana@x.com' y 'ana@x.com' registradas por separado), se deja la más antigua
    y se avisa por log en vez de fallar el arranque."""
    from .database import SessionLocal
    db = SessionLocal()
    try:
        usuarios = db.query(models.Usuario).order_by(models.Usuario.id).all()
        vistos = {}
        for u in usuarios:
            normalizado = _norm_email(u.email)
            if normalizado == u.email:
                vistos.setdefault(normalizado, u.id)
                continue
            if normalizado in vistos:
                print(f"[normalizar-email] Conflicto: usuario {u.id} ({u.email!r}) coincide "
                      f"con el usuario {vistos[normalizado]} tras normalizar. No se modifica, revísalo a mano.")
                continue
            u.email = normalizado
            vistos[normalizado] = u.id
        db.commit()
    except Exception as e:
        print(f"[normalizar-email] Error: {e}")
        db.rollback()
    finally:
        db.close()

_normalizar_emails_existentes()

def _crear_admin_inicial():
    """Si ADMIN_EMAIL y ADMIN_PASSWORD están definidos, crea (o promociona) esa cuenta
    como administrador al arrancar. Es la forma de dar de alta el primer admin sin
    exponer la opción en el registro público."""
    admin_email = _norm_email(os.getenv("ADMIN_EMAIL", ""))
    admin_pass = os.getenv("ADMIN_PASSWORD", "")
    if not admin_email or not admin_pass:
        return
    from .database import SessionLocal
    db = SessionLocal()
    try:
        usuario = db.query(models.Usuario).filter(models.Usuario.email == admin_email).first()
        if usuario:
            if usuario.tipo != "admin":
                usuario.tipo = "admin"
                db.commit()
        else:
            db.add(models.Usuario(
                nombre="Administrador",
                email=admin_email,
                telefono="",
                password_hash=auth.hashear_password(admin_pass),
                tipo="admin",
                terminos_aceptados=True,
                email_verificado=True,
            ))
            db.commit()
    finally:
        db.close()

_crear_admin_inicial()

app = FastAPI(title="Multiservicios Provenza API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def get_or_create_fontanero(db: Session, usuario_id: int, gremio: str = "fontanero"):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == usuario_id
    ).first()
    if fontanero:
        return fontanero
    usuario_obj = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if usuario_obj and usuario_obj.tipo == "fontanero":
        fontanero = models.Fontanero(
            usuario_id=usuario_id,
            nombre=usuario_obj.nombre,
            telefono=usuario_obj.telefono,
            disponible=True,
            disponible_24h=False,
            zona="Bilbao",
            gremio=gremio if gremio in GREMIOS_VALIDOS else "fontanero",
        )
        db.add(fontanero)
        db.commit()
        db.refresh(fontanero)
        return fontanero
    return None

def _crear_notificacion(db: Session, usuario_id: int, titulo: str, cuerpo: str, tipo: str = None, referencia_id: int = None):
    notif = models.Notificacion(
        usuario_id=usuario_id,
        titulo=titulo,
        cuerpo=cuerpo,
        tipo=tipo,
        referencia_id=referencia_id,
    )
    db.add(notif)
    db.flush()
    tokens = db.query(models.TokenPush).filter(
        models.TokenPush.usuario_id == usuario_id,
        models.TokenPush.activo == True,
    ).all()
    for t in tokens:
        # Los tokens se registran con expo-notifications (formato ExponentPushToken[...]),
        # así que se mandan al servicio push de Expo, no a FCM directo.
        try:
            _http.post(
                "https://exp.host/--/api/v2/push/send",
                json={
                    "to": t.token,
                    "title": titulo,
                    "body": cuerpo,
                    "sound": "default",
                    "data": {"tipo": tipo, "referencia_id": referencia_id},
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=5,
            )
        except Exception:
            pass

# ─── AUTH ──────────────────────────────────────────────────────────────────────

@app.get("/")
def inicio():
    return {"mensaje": "Multiservicios Provenza API funcionando"}

@app.post("/migrar-db")
def migrar_db():
    from .database import engine
    from sqlalchemy import text
    columnas = [
        ("fontaneros", "descripcion", "TEXT"),
        ("fontaneros", "especialidades", "TEXT"),
        ("fontaneros", "vacaciones_desde", "TIMESTAMP"),
        ("fontaneros", "vacaciones_hasta", "TIMESTAMP"),
        ("fontaneros", "gremio", "VARCHAR DEFAULT 'fontanero'"),
        ("fontaneros", "verificado", "BOOLEAN DEFAULT FALSE"),
        ("fontaneros", "num_trabajos", "INTEGER DEFAULT 0"),
        ("fontaneros", "disponible_24h", "BOOLEAN DEFAULT FALSE"),
        ("fontaneros", "foto_url", "VARCHAR"),
        ("servicios", "urgencia_ia", "VARCHAR"),
        ("servicios", "eta_minutos", "INTEGER"),
        ("servicios", "comision_aplicada", "FLOAT"),
        ("servicios", "stripe_payment_intent", "VARCHAR"),
    ]
    resultados = []
    with engine.connect() as conn:
        for tabla, columna, tipo in columnas:
            try:
                conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS {columna} {tipo}"))
                conn.commit()
                resultados.append(f"OK: {tabla}.{columna}")
            except Exception as ex:
                resultados.append(f"SKIP: {tabla}.{columna} ({ex})")
    return {"resultado": resultados}

@app.post("/registro", response_model=schemas.Token)
def registrar_usuario(usuario: schemas.UsuarioRegistro, db: Session = Depends(get_db)):
    email = _norm_email(usuario.email)
    existe = db.query(models.Usuario).filter(models.Usuario.email == email).first()
    if existe:
        raise HTTPException(status_code=400, detail="Email ya registrado")
    nuevo = models.Usuario(
        nombre=usuario.nombre,
        email=email,
        telefono=usuario.telefono,
        password_hash=auth.hashear_password(usuario.password),
        tipo=usuario.tipo,
        terminos_aceptados=usuario.terminos_aceptados,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)

    if usuario.tipo == "fontanero":
        get_or_create_fontanero(db, nuevo.id, usuario.gremio)
        _avisar_admin_nuevo_profesional(nuevo.nombre, nuevo.email, usuario.gremio)

    token_verificacion = secrets.token_hex(4).upper()
    db.add(models.VerificacionEmail(
        usuario_id=nuevo.id, token=token_verificacion,
        expira=datetime.datetime.utcnow() + datetime.timedelta(minutes=30),
    ))
    db.commit()
    _enviar_email_verificacion(nuevo.email, token_verificacion)

    token = auth.crear_token({"sub": nuevo.email, "tipo": nuevo.tipo, "id": nuevo.id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "tipo_usuario": nuevo.tipo,
        "nombre": nuevo.nombre,
        "id": nuevo.id,
        "email": nuevo.email,
    }

# Protección contra fuerza bruta: tras varios fallos seguidos con un mismo email,
# se bloquea temporalmente el login de ese email. En memoria (se reinicia con el proceso).
MAX_INTENTOS_LOGIN = 5
BLOQUEO_LOGIN_MINUTOS = 15
_intentos_login = {}  # email -> {"fallos": int, "bloqueado_hasta": datetime | None}

def _login_bloqueado(email: str):
    registro = _intentos_login.get(email)
    if not registro or not registro.get("bloqueado_hasta"):
        return None
    restante = registro["bloqueado_hasta"] - datetime.datetime.utcnow()
    if restante.total_seconds() <= 0:
        _intentos_login.pop(email, None)
        return None
    return int(restante.total_seconds() // 60) + 1

def _registrar_fallo_login(email: str):
    registro = _intentos_login.setdefault(email, {"fallos": 0, "bloqueado_hasta": None})
    registro["fallos"] += 1
    if registro["fallos"] >= MAX_INTENTOS_LOGIN:
        registro["bloqueado_hasta"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=BLOQUEO_LOGIN_MINUTOS)

@app.post("/login", response_model=schemas.Token)
def login(datos: schemas.UsuarioLogin, db: Session = Depends(get_db)):
    email_normalizado = _norm_email(datos.email)
    minutos = _login_bloqueado(email_normalizado)
    if minutos:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos. Inténtalo de nuevo en {minutos} min",
        )
    usuario = db.query(models.Usuario).filter(models.Usuario.email == email_normalizado).first()
    if not usuario or not auth.verificar_password(datos.password, usuario.password_hash):
        _registrar_fallo_login(email_normalizado)
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    if usuario.bloqueado:
        raise HTTPException(status_code=403, detail="Esta cuenta ha sido suspendida. Contacta con soporte")
    _intentos_login.pop(email_normalizado, None)
    token = auth.crear_token({"sub": usuario.email, "tipo": usuario.tipo, "id": usuario.id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "tipo_usuario": usuario.tipo,
        "nombre": usuario.nombre,
        "id": usuario.id,
        "email": usuario.email,
    }

def _enviar_email(destinatario: str, asunto: str, cuerpo: str, etiqueta: str, codigo: str = None):
    if not SMTP_HOST:
        print(f"[{etiqueta}] SMTP no configurado. Código para {destinatario}: {codigo or '(ver cuerpo)'}")
        return
    try:
        msg = MIMEText(cuerpo)
        msg["Subject"] = asunto
        msg["From"] = SMTP_FROM
        msg["To"] = destinatario
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [destinatario], msg.as_string())
    except Exception as e:
        print(f"[{etiqueta}] Error enviando email a {destinatario}: {e}")

def _enviar_email_reset(destinatario: str, token: str):
    cuerpo = (
        f"Tu código para restablecer tu contraseña en Multiservicios Provenza es:\n\n{token}\n\n"
        "Caduca en 30 minutos. Si no lo solicitaste, ignora este correo."
    )
    _enviar_email(destinatario, "Recupera tu contraseña — Multiservicios Provenza", cuerpo, "reset-password", token)

def _enviar_email_verificacion(destinatario: str, token: str):
    cuerpo = (
        f"Tu código para verificar tu email en Multiservicios Provenza es:\n\n{token}\n\n"
        "Caduca en 30 minutos. Si no creaste esta cuenta, ignora este correo."
    )
    _enviar_email(destinatario, "Verifica tu email — Multiservicios Provenza", cuerpo, "verificar-email", token)

def _avisar_admin_nuevo_profesional(nombre: str, email: str, gremio: str):
    """Avisa por email al administrador cuando se registra un profesional nuevo,
    para que sepa que hay alguien pendiente de verificar."""
    admin_email = _norm_email(os.getenv("ADMIN_EMAIL", ""))
    if not admin_email:
        return
    cuerpo = (
        f"Nuevo profesional registrado en Multiservicios Provenza:\n\n"
        f"Nombre: {nombre}\nEmail: {email}\nGremio: {gremio}\n\n"
        f"Revísalo y verifícalo desde el panel de administración."
    )
    _enviar_email(admin_email, "Nuevo profesional registrado — Multiservicios Provenza", cuerpo, "nuevo-profesional")

class OlvidePasswordDatos(BaseModel):
    email: str

@app.post("/auth/olvide-password")
def olvide_password(datos: OlvidePasswordDatos, db: Session = Depends(get_db)):
    usuario = db.query(models.Usuario).filter(models.Usuario.email == _norm_email(datos.email)).first()
    if usuario:
        token = secrets.token_hex(4).upper()
        reset = models.PasswordReset(
            usuario_id=usuario.id, token=token,
            expira=datetime.datetime.utcnow() + datetime.timedelta(minutes=30),
        )
        db.add(reset)
        db.commit()
        _enviar_email_reset(usuario.email, token)
    # Respuesta genérica siempre, para no revelar si el email existe o no
    return {"mensaje": "Si el email existe, se envió un código de recuperación"}

class ResetPasswordDatos(BaseModel):
    email: str
    token: str
    nueva_password: str

@app.post("/auth/resetear-password")
def resetear_password(datos: ResetPasswordDatos, db: Session = Depends(get_db)):
    usuario = db.query(models.Usuario).filter(models.Usuario.email == _norm_email(datos.email)).first()
    error_generico = HTTPException(status_code=400, detail="Código inválido o caducado")
    if not usuario:
        raise error_generico
    reset = db.query(models.PasswordReset).filter(
        models.PasswordReset.usuario_id == usuario.id,
        models.PasswordReset.token == datos.token.strip().upper(),
        models.PasswordReset.usado == False,
    ).order_by(models.PasswordReset.id.desc()).first()
    if not reset or reset.expira < datetime.datetime.utcnow():
        raise error_generico
    if len(datos.nueva_password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
    usuario.password_hash = auth.hashear_password(datos.nueva_password)
    reset.usado = True
    db.commit()
    return {"mensaje": "Contraseña actualizada"}

class VerificarEmailDatos(BaseModel):
    email: str
    token: str

@app.post("/auth/verificar-email")
def verificar_email(datos: VerificarEmailDatos, db: Session = Depends(get_db)):
    usuario = db.query(models.Usuario).filter(models.Usuario.email == _norm_email(datos.email)).first()
    error_generico = HTTPException(status_code=400, detail="Código inválido o caducado")
    if not usuario:
        raise error_generico
    if usuario.email_verificado:
        return {"mensaje": "Email ya verificado"}
    verificacion = db.query(models.VerificacionEmail).filter(
        models.VerificacionEmail.usuario_id == usuario.id,
        models.VerificacionEmail.token == datos.token.strip().upper(),
        models.VerificacionEmail.usado == False,
    ).order_by(models.VerificacionEmail.id.desc()).first()
    if not verificacion or verificacion.expira < datetime.datetime.utcnow():
        raise error_generico
    usuario.email_verificado = True
    verificacion.usado = True
    db.commit()
    return {"mensaje": "Email verificado"}

class ReenviarVerificacionDatos(BaseModel):
    email: str

@app.post("/auth/reenviar-verificacion")
def reenviar_verificacion(datos: ReenviarVerificacionDatos, db: Session = Depends(get_db)):
    usuario = db.query(models.Usuario).filter(models.Usuario.email == _norm_email(datos.email)).first()
    if usuario and not usuario.email_verificado:
        token = secrets.token_hex(4).upper()
        db.add(models.VerificacionEmail(
            usuario_id=usuario.id, token=token,
            expira=datetime.datetime.utcnow() + datetime.timedelta(minutes=30),
        ))
        db.commit()
        _enviar_email_verificacion(usuario.email, token)
    return {"mensaje": "Si el email existe y no está verificado, se envió un nuevo código"}

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

class GoogleAuthDatos(BaseModel):
    id_token: str
    tipo: Optional[str] = "cliente"

@app.post("/auth/google", response_model=schemas.Token)
def login_google(datos: GoogleAuthDatos, db: Session = Depends(get_db)):
    """Verifica el id_token que devuelve Google en el flujo de Sign in with Google
    (vía expo-auth-session en el frontend) contra el propio endpoint de Google,
    y crea la cuenta o inicia sesión si ya existe."""
    try:
        resp = _http.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": datos.id_token},
            timeout=8,
        )
        info = resp.json()
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo verificar el token de Google")
    if resp.status_code != 200 or "email" not in info:
        raise HTTPException(status_code=401, detail="Token de Google inválido")
    if GOOGLE_CLIENT_ID and info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Token de Google no corresponde a esta app")

    email = _norm_email(info["email"])
    nombre = info.get("name") or email.split("@")[0]
    usuario = db.query(models.Usuario).filter(models.Usuario.email == email).first()
    if not usuario:
        usuario = models.Usuario(
            nombre=nombre,
            email=email,
            telefono="",
            password_hash=auth.hashear_password(secrets.token_hex(16)),
            tipo=datos.tipo if datos.tipo in ["cliente", "fontanero"] else "cliente",
            terminos_aceptados=True,
            email_verificado=True,  # Google ya verificó este email
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)
        if usuario.tipo == "fontanero":
            get_or_create_fontanero(db, usuario.id)
            _avisar_admin_nuevo_profesional(usuario.nombre, usuario.email, "fontanero")

    token = auth.crear_token({"sub": usuario.email, "tipo": usuario.tipo, "id": usuario.id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "tipo_usuario": usuario.tipo,
        "nombre": usuario.nombre,
        "id": usuario.id,
        "email": usuario.email,
    }

# ─── MI CUENTA ─────────────────────────────────────────────────────────────────

@app.get("/usuarios/{usuario_id}/perfil")
def ver_perfil_usuario(
    usuario_id: int,
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes ver otra cuenta")
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return {
        "nombre": usuario.nombre,
        "email": usuario.email,
        "telefono": usuario.telefono or "",
        "tipo": usuario.tipo,
        "email_verificado": usuario.email_verificado,
    }

class PerfilUsuarioDatos(BaseModel):
    nombre: Optional[str] = None
    telefono: Optional[str] = None

@app.put("/usuarios/{usuario_id}/perfil")
def actualizar_perfil_usuario(
    usuario_id: int,
    datos: PerfilUsuarioDatos,
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes editar otra cuenta")
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if datos.nombre is not None:
        nombre = datos.nombre.strip()
        if not nombre:
            raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")
        usuario.nombre = nombre
    if datos.telefono is not None:
        usuario.telefono = datos.telefono.strip()
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario_id).first()
    if fontanero:
        if datos.nombre is not None:
            fontanero.nombre = usuario.nombre
        if datos.telefono is not None:
            fontanero.telefono = usuario.telefono
    db.commit()
    return {"mensaje": "Perfil actualizado", "nombre": usuario.nombre, "telefono": usuario.telefono}

class CambiarPasswordDatos(BaseModel):
    password_actual: str
    password_nueva: str

@app.put("/usuarios/{usuario_id}/password")
def cambiar_password(
    usuario_id: int,
    datos: CambiarPasswordDatos,
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes cambiar la contraseña de otra cuenta")
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    # 400 y no 401: el 401 con token adjunto lo interpreta la app como sesión caducada
    if not auth.verificar_password(datos.password_actual, usuario.password_hash):
        raise HTTPException(status_code=400, detail="La contraseña actual no es correcta")
    if len(datos.password_nueva) < 6:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 6 caracteres")
    usuario.password_hash = auth.hashear_password(datos.password_nueva)
    db.commit()
    return {"mensaje": "Contraseña actualizada"}

@app.delete("/usuarios/{usuario_id}")
def eliminar_cuenta(
    usuario_id: int,
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Elimina la cuenta propia: borra los datos personales y anonimiza el historial
    (los servicios y reseñas se conservan sin datos identificativos, RGPD)."""
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes eliminar otra cuenta")
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    db.query(models.TokenPush).filter(models.TokenPush.usuario_id == usuario_id).delete()
    db.query(models.Notificacion).filter(models.Notificacion.usuario_id == usuario_id).delete()
    db.query(models.Favorito).filter(models.Favorito.cliente_id == usuario_id).delete()
    db.query(models.VerificacionEmail).filter(models.VerificacionEmail.usuario_id == usuario_id).delete()
    db.query(models.PasswordReset).filter(models.PasswordReset.usuario_id == usuario_id).delete()

    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario_id).first()
    if fontanero:
        fontanero.nombre = "Fontanero eliminado"
        fontanero.telefono = ""
        fontanero.disponible = False
        fontanero.disponible_24h = False
        fontanero.foto_url = None
        fontanero.descripcion = None
        fontanero.latitud = None
        fontanero.longitud = None

    usuario.nombre = "Usuario eliminado"
    usuario.email = f"eliminado-{usuario_id}-{secrets.token_hex(4)}@cuenta-eliminada.fontap"
    usuario.telefono = ""
    usuario.password_hash = auth.hashear_password(secrets.token_hex(16))
    usuario.email_verificado = False
    db.commit()
    return {"mensaje": "Cuenta eliminada"}

# ─── FONTANEROS ────────────────────────────────────────────────────────────────

@app.get("/fontaneros", response_model=List[schemas.FontaneroRespuesta])
def listar_fontaneros(gremio: Optional[str] = None, db: Session = Depends(get_db)):
    # Excluye profesionales cuyo usuario esté vetado por el admin
    query = db.query(models.Fontanero).outerjoin(
        models.Usuario, models.Fontanero.usuario_id == models.Usuario.id
    ).filter(
        models.Fontanero.disponible == True,
        or_(models.Usuario.bloqueado == False, models.Usuario.bloqueado == None, models.Fontanero.usuario_id == None),
    )
    if gremio:
        query = query.filter(models.Fontanero.gremio == gremio)
    fontaneros = query.all()

    from sqlalchemy import func
    precios_min = dict(
        db.query(models.ServicioFontanero.fontanero_id, func.min(models.ServicioFontanero.precio))
        .filter(models.ServicioFontanero.activo == True)
        .group_by(models.ServicioFontanero.fontanero_id)
        .all()
    )
    resultado = []
    for f in fontaneros:
        item = schemas.FontaneroRespuesta.model_validate(f)
        item.precio_desde = precios_min.get(f.id)
        resultado.append(item)
    return resultado

class DisponibilidadUpdate(BaseModel):
    disponible: bool
    disponible_24h: Optional[bool] = None

@app.put("/fontaneros/{fontanero_id}/disponibilidad")
def actualizar_disponibilidad(
    fontanero_id: int,
    datos: DisponibilidadUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.disponible = datos.disponible
    if datos.disponible_24h is not None:
        fontanero.disponible_24h = datos.disponible_24h
    db.commit()
    return {"mensaje": "Disponibilidad actualizada"}

@app.put("/fontaneros/{fontanero_id}/ubicacion")
def actualizar_ubicacion(
    fontanero_id: int,
    datos: schemas.UbicacionActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = get_or_create_fontanero(db, fontanero_id)
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.latitud = datos.latitud
    fontanero.longitud = datos.longitud
    fontanero.ubicacion_actualizada = models.utcnow()
    db.commit()
    return {"mensaje": "Ubicación actualizada"}

@app.get("/fontaneros/{fontanero_id}/solicitudes")
def ver_solicitudes_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = get_or_create_fontanero(db, fontanero_id)
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    pendientes = db.query(models.Servicio).filter(
        models.Servicio.estado == "pendiente",
        or_(
            models.Servicio.fontanero_id == None,
            models.Servicio.fontanero_id == fontanero.id,
        ),
    ).all()

    propias = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado.in_(["aceptado", "precio_enviado", "pago_pendiente", "pagado", "completado"]),
    ).all()

    resultado = []
    for s in pendientes + propias:
        cliente = db.query(models.Usuario).filter(models.Usuario.id == s.cliente_id).first()
        resultado.append({
            "id": s.id,
            "tipo": s.tipo,
            "descripcion": s.descripcion,
            "urgente": s.urgente,
            "estado": s.estado,
            "estado_color": schemas.ESTADO_COLORES.get(s.estado, "#D97706"),
            "precio": s.precio,
            "fecha": str(s.fecha) if s.fecha else None,
            "cliente_nombre": cliente.nombre if cliente else "Cliente",
            "zona": "Bilbao",
        })
    return resultado

# ─── SERVICIOS ─────────────────────────────────────────────────────────────────

@app.post("/servicios", response_model=schemas.ServicioRespuesta)
def crear_servicio(
    servicio: schemas.ServicioCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    cliente_id = current_user["id"]
    nuevo = models.Servicio(
        cliente_id=cliente_id,
        fontanero_id=servicio.fontanero_id,
        tipo=servicio.tipo,
        descripcion=servicio.descripcion,
        urgente=servicio.urgente,
        fecha=servicio.fecha,
        estado="pendiente",
        precio=None,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    if servicio.fontanero_id:
        fontanero_directo = db.query(models.Fontanero).filter(
            models.Fontanero.id == servicio.fontanero_id
        ).first()
        if fontanero_directo:
            _crear_notificacion(db, fontanero_directo.usuario_id, "Nueva solicitud", f"Tienes una nueva solicitud de {servicio.tipo}", "solicitud_directa", nuevo.id)
            db.commit()
    elif servicio.urgente:
        fontaneros_zona = db.query(models.Fontanero).filter(
            models.Fontanero.disponible == True,
            models.Fontanero.usuario_id != None,
        ).all()
        for f in fontaneros_zona:
            _crear_notificacion(db, f.usuario_id, "Nueva solicitud urgente", f"Solicitud urgente de {servicio.tipo} cerca de tu zona", "solicitud_urgente", nuevo.id)
        db.commit()
    return schemas.ServicioRespuesta.from_orm_with_color(nuevo)

@app.get("/servicios/abiertos", response_model=List[schemas.ServicioRespuesta])
def listar_servicios_abiertos(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicios = db.query(models.Servicio).filter(
        models.Servicio.estado == "pendiente",
        models.Servicio.fontanero_id == None,
    ).order_by(models.Servicio.id.desc()).all()
    return [schemas.ServicioRespuesta.from_orm_with_color(s) for s in servicios]

@app.get("/servicios/{servicio_id}")
def ver_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    return schemas.ServicioRespuesta.from_orm_with_color(servicio)

@app.put("/servicios/{servicio_id}/aceptar")
def aceptar_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero_usuario_id = current_user["id"]
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_usuario_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if servicio.fontanero_id is not None and servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=400, detail="Este servicio ya fue asignado a otro profesional")
    servicio.fontanero_id = fontanero.id
    servicio.estado = "aceptado"
    _crear_notificacion(db, servicio.cliente_id, "Servicio aceptado", f"Un fontanero ha aceptado tu solicitud de {servicio.tipo}", "servicio_aceptado", servicio.id)
    db.commit()
    return {"mensaje": "Servicio aceptado"}

@app.put("/servicios/{servicio_id}/rechazar")
def rechazar_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id not in (None, fontanero.id):
        raise HTTPException(status_code=403, detail="No puedes rechazar un servicio que no es tuyo")
    servicio.estado = "rechazado"
    _crear_notificacion(db, servicio.cliente_id, "Servicio rechazado", f"Tu solicitud de {servicio.tipo} no pudo ser atendida", "servicio_rechazado", servicio.id)
    db.commit()
    return {"mensaje": "Servicio rechazado"}

class PrecioUpdate(BaseModel):
    precio: float

@app.put("/servicios/{servicio_id}/precio")
def enviar_precio(
    servicio_id: int,
    datos: PrecioUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="No puedes enviar precio para un servicio que no es tuyo")
    servicio.precio = datos.precio
    servicio.estado = "precio_enviado"
    _crear_notificacion(db, servicio.cliente_id, "Precio recibido", f"El fontanero ha enviado un presupuesto de {datos.precio}€", "precio_enviado", servicio.id)
    db.commit()
    return {"mensaje": "Precio enviado al cliente", "precio": datos.precio}

class PagoUpdate(BaseModel):
    metodo: str

@app.put("/servicios/{servicio_id}/pagar")
def confirmar_pago(
    servicio_id: int,
    datos: PagoUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede confirmar el pago de este servicio")
    if not servicio.precio:
        raise HTTPException(status_code=400, detail="El fontanero aún no ha enviado el precio")
    nuevo_estado = "pago_pendiente" if datos.metodo == "efectivo" else "pagado"
    servicio.estado = nuevo_estado
    servicio.metodo_pago = datos.metodo
    if datos.metodo not in ["efectivo", "stripe"]:
        # El dinero (ej. Bizum) va directo cliente→fontanero, sin pasar por Multiservicios Provenza:
        # queda pendiente que el fontanero liquide la comisión de la plataforma.
        servicio.comision_aplicada = round(servicio.precio * 0.05, 2)
        servicio.comision_liquidada = False
    if servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Pago recibido", f"El cliente ha confirmado el pago de {servicio.precio}€ via {datos.metodo}", "pago_recibido", servicio.id)
    db.commit()
    return {"mensaje": "Pago registrado", "precio": servicio.precio, "metodo": datos.metodo}

@app.put("/servicios/{servicio_id}/confirmar_efectivo")
def confirmar_efectivo(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.estado = "pagado"
    # El efectivo va directo cliente→fontanero: la comisión de Multiservicios Provenza queda
    # pendiente de que el fontanero la liquide (ver /comision-pendiente).
    servicio.comision_aplicada = round((servicio.precio or 0) * 0.05, 2)
    servicio.comision_liquidada = False
    db.commit()
    return {"mensaje": "Efectivo confirmado"}

@app.put("/servicios/{servicio_id}/cancelar")
def cancelar_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    es_cliente = servicio.cliente_id == current_user["id"]
    fontanero_actual = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    es_fontanero = bool(fontanero_actual) and servicio.fontanero_id == fontanero_actual.id
    if not es_cliente and not es_fontanero:
        raise HTTPException(status_code=403, detail="No puedes cancelar un servicio que no es tuyo")
    if servicio.estado in ["pagado", "completado", "cancelado"]:
        raise HTTPException(status_code=400, detail="Este servicio ya no se puede cancelar")

    servicio.estado = "cancelado"
    db.commit()

    if es_cliente and servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Servicio cancelado", "El cliente ha cancelado la solicitud", "cancelado", servicio_id)
    elif es_fontanero:
        _crear_notificacion(db, servicio.cliente_id, "Servicio cancelado", "El profesional ha cancelado el servicio", "cancelado", servicio_id)
    db.commit()
    return {"mensaje": "Servicio cancelado"}

@app.get("/clientes/{cliente_id}/servicios")
def ver_servicios_cliente(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicios = db.query(models.Servicio).filter(
        models.Servicio.cliente_id == cliente_id
    ).order_by(models.Servicio.id.desc()).all()
    resultado = []
    for s in servicios:
        data = schemas.ServicioRespuesta.from_orm_with_color(s)
        if s.fontanero_id:
            fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == s.fontanero_id).first()
            data.fontanero_nombre = fontanero.nombre if fontanero else None
        if s.estado == "pendiente":
            data.num_ofertas = db.query(models.Oferta).filter(
                models.Oferta.servicio_id == s.id,
                models.Oferta.estado == "pendiente",
            ).count()
        resultado.append(data)
    return resultado

# ─── HORARIOS / BLOQUEOS / SERVICIOS FONTANERO ────────────────────────────────

def _resolver_fontanero(db: Session, fontanero_id: int) -> models.Fontanero:
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return fontanero

@app.post("/fontaneros/{fontanero_id}/horario", response_model=schemas.HorarioBaseRespuesta)
def crear_horario(
    fontanero_id: int,
    horario: schemas.HorarioBaseCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    nuevo = models.HorarioBase(
        fontanero_id=fontanero.id,
        dia_semana=horario.dia_semana,
        hora_inicio=horario.hora_inicio,
        hora_fin=horario.hora_fin,
        intervalo_minutos=horario.intervalo_minutos,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/horario", response_model=List[schemas.HorarioBaseRespuesta])
def ver_horario(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = _resolver_fontanero(db, fontanero_id)
    return db.query(models.HorarioBase).filter(
        models.HorarioBase.fontanero_id == fontanero.id
    ).all()

@app.post("/fontaneros/{fontanero_id}/bloqueos", response_model=schemas.BloqueoRespuesta)
def crear_bloqueo(
    fontanero_id: int,
    bloqueo: schemas.BloqueoCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    nuevo = models.BloqueoHorario(
        fontanero_id=fontanero.id,
        fecha=bloqueo.fecha,
        hora_inicio=bloqueo.hora_inicio,
        hora_fin=bloqueo.hora_fin,
        motivo=bloqueo.motivo,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/bloqueos", response_model=List[schemas.BloqueoRespuesta])
def ver_bloqueos(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    return db.query(models.BloqueoHorario).filter(
        models.BloqueoHorario.fontanero_id == fontanero.id
    ).all()

@app.delete("/fontaneros/{fontanero_id}/bloqueos/{bloqueo_id}")
def eliminar_bloqueo(
    fontanero_id: int,
    bloqueo_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    bloqueo = db.query(models.BloqueoHorario).filter(
        models.BloqueoHorario.id == bloqueo_id,
        models.BloqueoHorario.fontanero_id == fontanero.id,
    ).first()
    if not bloqueo:
        raise HTTPException(status_code=404, detail="Bloqueo no encontrado")
    db.delete(bloqueo)
    db.commit()
    return {"mensaje": "Bloqueo eliminado"}

@app.post("/fontaneros/{fontanero_id}/servicios", response_model=schemas.ServicioFontaneroRespuesta)
def añadir_servicio(
    fontanero_id: int,
    servicio: schemas.ServicioFontaneroCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    nuevo = models.ServicioFontanero(
        fontanero_id=fontanero.id,
        nombre=servicio.nombre,
        precio=servicio.precio,
        duracion_minutos=servicio.duracion_minutos,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/servicios", response_model=List[schemas.ServicioFontaneroRespuesta])
def ver_servicios_fontanero(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = _resolver_fontanero(db, fontanero_id)
    return db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.fontanero_id == fontanero.id,
        models.ServicioFontanero.activo == True,
    ).all()

@app.delete("/fontaneros/{fontanero_id}/servicios/{servicio_id}")
def eliminar_servicio(
    fontanero_id: int,
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    servicio = db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.id == servicio_id,
        models.ServicioFontanero.fontanero_id == fontanero.id,
    ).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.activo = False
    db.commit()
    return {"mensaje": "Servicio eliminado"}

# ─── IMÁGENES DE SERVICIO ──────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

@app.post("/servicios/{servicio_id}/imagenes", response_model=schemas.ImagenRespuesta)
def subir_imagen_servicio(
    servicio_id: int,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "jpg"
    nombre_archivo = f"{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    imagen = models.ImagenServicio(servicio_id=servicio_id, url=f"/uploads/{nombre_archivo}")
    db.add(imagen)
    db.commit()
    db.refresh(imagen)
    return imagen

@app.get("/servicios/{servicio_id}/imagenes", response_model=List[schemas.ImagenRespuesta])
def listar_imagenes_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.ImagenServicio).filter(
        models.ImagenServicio.servicio_id == servicio_id
    ).all()

# ─── CHAT ──────────────────────────────────────────────────────────────────────

@app.put("/servicios/{servicio_id}/mensajes/leer")
def marcar_mensajes_leidos(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.Mensaje).filter(
        models.Mensaje.servicio_id == servicio_id,
        models.Mensaje.emisor_id != current_user["id"],
        models.Mensaje.leido == False,
    ).update({"leido": True})
    db.commit()
    return {"mensaje": "Mensajes marcados como leídos"}

@app.get("/usuarios/{usuario_id}/chats")
def listar_chats_recientes(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario_id).first()
    fontanero_id = fontanero.id if fontanero else None
    servicios = db.query(models.Servicio).filter(
        or_(
            models.Servicio.cliente_id == usuario_id,
            models.Servicio.fontanero_id == fontanero_id,
        )
    ).all()

    resultado = []
    for s in servicios:
        ultimo = db.query(models.Mensaje).filter(
            models.Mensaje.servicio_id == s.id
        ).order_by(models.Mensaje.creado_en.desc()).first()
        if not ultimo:
            continue
        no_leidos = db.query(models.Mensaje).filter(
            models.Mensaje.servicio_id == s.id,
            models.Mensaje.emisor_id != usuario_id,
            models.Mensaje.leido == False,
        ).count()
        if usuario_id == s.cliente_id:
            otro_nombre = "Profesional"
            if s.fontanero_id:
                fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == s.fontanero_id).first()
                if fontanero_obj and fontanero_obj.nombre:
                    otro_nombre = fontanero_obj.nombre
        else:
            cliente_obj = db.query(models.Usuario).filter(models.Usuario.id == s.cliente_id).first()
            otro_nombre = cliente_obj.nombre if cliente_obj else "Cliente"
        resultado.append({
            "servicio_id": s.id,
            "tipo_servicio": s.tipo,
            "estado": s.estado,
            "otro_participante": otro_nombre,
            "ultimo_mensaje": ultimo.texto,
            "ultimo_mensaje_fecha": str(ultimo.creado_en),
            "no_leidos": no_leidos,
        })
    resultado.sort(key=lambda c: c["ultimo_mensaje_fecha"], reverse=True)
    return resultado

# ─── NOTIFICACIONES PUSH ───────────────────────────────────────────────────────

@app.post("/usuarios/{usuario_id}/push-token")
def registrar_push_token(
    usuario_id: int,
    datos: schemas.TokenPushCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    existente = db.query(models.TokenPush).filter(models.TokenPush.token == datos.token).first()
    if existente:
        existente.usuario_id = usuario_id
        existente.activo = True
        existente.plataforma = datos.plataforma
    else:
        db.add(models.TokenPush(usuario_id=usuario_id, token=datos.token, plataforma=datos.plataforma))
    db.commit()
    return {"mensaje": "Token registrado"}

@app.delete("/usuarios/{usuario_id}/push-token")
def eliminar_push_token(
    usuario_id: int,
    datos: schemas.TokenPushCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.TokenPush).filter(
        models.TokenPush.usuario_id == usuario_id,
        models.TokenPush.token == datos.token,
    ).update({"activo": False})
    db.commit()
    return {"mensaje": "Token eliminado"}

@app.get("/usuarios/{usuario_id}/notificaciones", response_model=List[schemas.NotificacionRespuesta])
def listar_notificaciones(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.Notificacion).filter(
        models.Notificacion.usuario_id == usuario_id
    ).order_by(models.Notificacion.creado_en.desc()).limit(50).all()

@app.put("/notificaciones/{notif_id}/leer")
def marcar_notificacion_leida(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    notif = db.query(models.Notificacion).filter(
        models.Notificacion.id == notif_id,
        models.Notificacion.usuario_id == current_user["id"],
    ).first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notificación no encontrada")
    notif.leida = True
    db.commit()
    return {"mensaje": "Notificación marcada como leída"}

@app.put("/usuarios/{usuario_id}/notificaciones/leer-todas")
def marcar_todas_leidas(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.Notificacion).filter(
        models.Notificacion.usuario_id == usuario_id,
        models.Notificacion.leida == False,
    ).update({"leida": True})
    db.commit()
    return {"mensaje": "Todas las notificaciones marcadas como leídas"}

# ─── PERFIL FONTANERO ──────────────────────────────────────────────────────────

@app.put("/fontaneros/{fontanero_id}/perfil")
def actualizar_perfil_fontanero(
    fontanero_id: int,
    datos: schemas.FontaneroActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if datos.zona is not None:
        fontanero.zona = datos.zona
    if datos.descripcion is not None:
        fontanero.descripcion = datos.descripcion
    if datos.especialidades is not None:
        fontanero.especialidades = datos.especialidades
    if datos.disponible_24h is not None:
        fontanero.disponible_24h = datos.disponible_24h
    db.commit()
    return {"mensaje": "Perfil actualizado"}

@app.post("/fontaneros/{fontanero_id}/foto")
def subir_foto_perfil(
    fontanero_id: int,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "jpg"
    nombre_archivo = f"perfil_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    fontanero.foto_url = f"/uploads/{nombre_archivo}"
    db.commit()
    return {"foto_url": fontanero.foto_url}

@app.get("/fontaneros/{fontanero_id}/perfil", response_model=schemas.FontaneroRespuesta)
def ver_perfil_fontanero(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return fontanero

# ─── VACACIONES ────────────────────────────────────────────────────────────────

@app.put("/fontaneros/{fontanero_id}/vacaciones")
def activar_vacaciones(
    fontanero_id: int,
    datos: schemas.VacacionesCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.vacaciones_desde = datos.desde
    fontanero.vacaciones_hasta = datos.hasta
    fontanero.disponible = False
    db.commit()
    return {"mensaje": "Modo vacaciones activado"}

@app.delete("/fontaneros/{fontanero_id}/vacaciones")
def cancelar_vacaciones(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.vacaciones_desde = None
    fontanero.vacaciones_hasta = None
    fontanero.disponible = True
    db.commit()
    return {"mensaje": "Modo vacaciones cancelado"}

# ─── GALERÍA DE TRABAJOS ───────────────────────────────────────────────────────

@app.post("/fontaneros/{fontanero_id}/galeria", response_model=schemas.GaleriaRespuesta)
def subir_foto_galeria(
    fontanero_id: int,
    descripcion: Optional[str] = None,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "jpg"
    nombre_archivo = f"galeria_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    foto = models.GaleriaFontanero(
        fontanero_id=fontanero.id,
        url=f"/uploads/{nombre_archivo}",
        descripcion=descripcion,
    )
    db.add(foto)
    db.commit()
    db.refresh(foto)
    return foto

@app.get("/fontaneros/{fontanero_id}/galeria", response_model=List[schemas.GaleriaRespuesta])
def ver_galeria(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.GaleriaFontanero).filter(
        models.GaleriaFontanero.fontanero_id == fontanero.id
    ).order_by(models.GaleriaFontanero.creado_en.desc()).all()

@app.delete("/fontaneros/{fontanero_id}/galeria/{foto_id}")
def eliminar_foto_galeria(
    fontanero_id: int,
    foto_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    foto = db.query(models.GaleriaFontanero).filter(
        models.GaleriaFontanero.id == foto_id,
        models.GaleriaFontanero.fontanero_id == fontanero.id,
    ).first()
    if not foto:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    if os.path.exists(foto.url.lstrip("/")):
        os.remove(foto.url.lstrip("/"))
    db.delete(foto)
    db.commit()
    return {"mensaje": "Foto eliminada"}

# ─── ESTADÍSTICAS ──────────────────────────────────────────────────────────────

@app.get("/fontaneros/{fontanero_id}/estadisticas", response_model=schemas.EstadisticasRespuesta)
def ver_estadisticas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    servicios_pagados = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "pagado",
    ).all()

    trabajos_completados = len(servicios_pagados)
    ingresos_totales = sum(s.precio for s in servicios_pagados if s.precio) * 0.95

    aceptados = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado.in_(["aceptado", "precio_enviado", "pagado", "pago_pendiente"]),
    ).count()
    rechazados = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "rechazado",
    ).count()
    total = aceptados + rechazados
    tasa_aceptacion = round(aceptados / total * 100, 1) if total > 0 else 100.0

    return {
        "trabajos_completados": trabajos_completados,
        "ingresos_totales": round(ingresos_totales, 2),
        "valoracion_media": fontanero.valoracion,
        "tasa_aceptacion": tasa_aceptacion,
    }

# ─── BÚSQUEDA Y FILTROS ────────────────────────────────────────────────────────

@app.get("/buscar/fontaneros", response_model=List[schemas.FontaneroRespuesta])
def buscar_fontaneros(
    zona: Optional[str] = None,
    gremio: Optional[str] = None,
    disponible_24h: Optional[bool] = None,
    verificado: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Fontanero).filter(models.Fontanero.disponible == True)
    if zona:
        q = q.filter(models.Fontanero.zona.ilike(f"%{zona}%"))
    if gremio:
        q = q.filter(models.Fontanero.gremio == gremio)
    if disponible_24h is not None:
        q = q.filter(models.Fontanero.disponible_24h == disponible_24h)
    if verificado is not None:
        q = q.filter(models.Fontanero.verificado == verificado)
    return q.order_by(nullslast(models.Fontanero.valoracion.desc())).all()

# ─── SEGUIMIENTO EN VIVO ───────────────────────────────────────────────────────

@app.put("/servicios/{servicio_id}/en-camino")
def marcar_en_camino(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El profesional marca que va de camino: cambia el estado y avisa al cliente."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="Este servicio no es tuyo")
    if servicio.estado not in ["aceptado", "precio_enviado"]:
        raise HTTPException(status_code=400, detail=f"No puedes marcar en camino un servicio en estado {servicio.estado}")
    servicio.estado = "en_camino"
    _crear_notificacion(db, servicio.cliente_id, "🚗 Tu profesional va en camino",
                        f"{fontanero.nombre} ya está de camino. Puedes seguirlo en el mapa desde Mis Servicios",
                        "en_camino", servicio_id)
    db.commit()
    return {"mensaje": "Marcado en camino", "estado": servicio.estado}

@app.get("/servicios/{servicio_id}/seguimiento")
def seguimiento_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Posición y ETA del profesional para que el cliente lo siga en el mapa."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    es_cliente = servicio.cliente_id == current_user["id"]
    fontanero_propio = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    es_fontanero = fontanero_propio and servicio.fontanero_id == fontanero_propio.id
    if not es_cliente and not es_fontanero:
        raise HTTPException(status_code=403, detail="No participas en este servicio")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
    return {
        "estado": servicio.estado,
        "eta_minutos": servicio.eta_minutos,
        "fontanero_nombre": fontanero.nombre if fontanero else None,
        "latitud": fontanero.latitud if fontanero else None,
        "longitud": fontanero.longitud if fontanero else None,
        "ubicacion_actualizada": fontanero.ubicacion_actualizada.isoformat() if fontanero and fontanero.ubicacion_actualizada else None,
    }

# ─── ETA ───────────────────────────────────────────────────────────────────────

@app.put("/servicios/{servicio_id}/eta")
def actualizar_eta(
    servicio_id: int,
    datos: schemas.ETAUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="No puedes actualizar el ETA de un servicio que no es tuyo")
    servicio.eta_minutos = datos.eta_minutos
    _crear_notificacion(db, servicio.cliente_id, "Fontanero en camino", f"Llegará en aproximadamente {datos.eta_minutos} minutos", "eta", servicio_id)
    db.commit()
    return {"mensaje": "ETA actualizado", "eta_minutos": datos.eta_minutos}

# ─── IA CLASIFICACIÓN URGENCIA ────────────────────────────────────────────────

PALABRAS_CRITICA = ["inundación", "inundacion", "rotura", "reventado", "agua por todas", "escape de gas", "gas"]
PALABRAS_ALTA = ["fuga", "goteo fuerte", "sin agua", "tubería rota", "tuberia rota", "urgente"]
PALABRAS_MEDIA = ["goteo", "grifo", "presión baja", "presion baja", "atasco"]

def _clasificar_urgencia(texto: str) -> str:
    t = (texto or "").lower()
    if any(p in t for p in PALABRAS_CRITICA):
        return "critica"
    if any(p in t for p in PALABRAS_ALTA):
        return "alta"
    if any(p in t for p in PALABRAS_MEDIA):
        return "media"
    return "baja"

@app.post("/servicios/{servicio_id}/clasificar-urgencia")
def clasificar_urgencia(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    nivel = _clasificar_urgencia(f"{servicio.tipo} {servicio.descripcion or ''}")
    servicio.urgencia_ia = nivel
    db.commit()
    return {"urgencia_ia": nivel}

# ─── FAVORITOS ─────────────────────────────────────────────────────────────────

@app.post("/clientes/{cliente_id}/favoritos/{fontanero_id}")
def agregar_favorito(
    cliente_id: int,
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    existe = db.query(models.Favorito).filter(
        models.Favorito.cliente_id == cliente_id,
        models.Favorito.fontanero_id == fontanero_id,
    ).first()
    if existe:
        return {"mensaje": "Ya está en favoritos"}
    db.add(models.Favorito(cliente_id=cliente_id, fontanero_id=fontanero_id))
    db.commit()
    return {"mensaje": "Añadido a favoritos"}

@app.delete("/clientes/{cliente_id}/favoritos/{fontanero_id}")
def eliminar_favorito(
    cliente_id: int,
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fav = db.query(models.Favorito).filter(
        models.Favorito.cliente_id == cliente_id,
        models.Favorito.fontanero_id == fontanero_id,
    ).first()
    if fav:
        db.delete(fav)
        db.commit()
    return {"mensaje": "Eliminado de favoritos"}

@app.get("/clientes/{cliente_id}/favoritos", response_model=List[schemas.FontaneroRespuesta])
def listar_favoritos(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    favs = db.query(models.Favorito).filter(models.Favorito.cliente_id == cliente_id).all()
    ids = [f.fontanero_id for f in favs]
    return db.query(models.Fontanero).filter(models.Fontanero.id.in_(ids)).all()

# ─── SISTEMA DE LICITACIÓN (OFERTAS) ──────────────────────────────────────────

def _enriquecer_oferta(db: Session, oferta: models.Oferta, incluir_fontanero: bool = False, incluir_servicio: bool = False) -> schemas.OfertaRespuesta:
    data = schemas.OfertaRespuesta.model_validate(oferta)
    if incluir_fontanero:
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == oferta.fontanero_id).first()
        if fontanero:
            data.fontanero_nombre = fontanero.nombre
            data.fontanero_valoracion = fontanero.valoracion
            data.fontanero_zona = fontanero.zona
            data.fontanero_trabajos = fontanero.num_trabajos
    if incluir_servicio:
        servicio = db.query(models.Servicio).filter(models.Servicio.id == oferta.servicio_id).first()
        if servicio:
            data.tipo = servicio.tipo
            data.zona = "Bilbao"
    return data

@app.post("/servicios/{servicio_id}/ofertas", response_model=schemas.OfertaRespuesta)
def crear_oferta(
    servicio_id: int,
    datos: schemas.OfertaCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    existente = db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.fontanero_id == fontanero.id,
    ).first()
    if existente:
        existente.precio = datos.precio
        existente.mensaje = datos.mensaje
        db.commit()
        db.refresh(existente)
        return existente
    oferta = models.Oferta(servicio_id=servicio_id, fontanero_id=fontanero.id, precio=datos.precio, mensaje=datos.mensaje)
    db.add(oferta)
    _crear_notificacion(db, servicio.cliente_id, "Nueva oferta recibida", f"Un profesional ofrece realizar el servicio por {datos.precio}€", "oferta", servicio_id)
    db.commit()
    db.refresh(oferta)
    return oferta

@app.get("/servicios/{servicio_id}/ofertas", response_model=List[schemas.OfertaRespuesta])
def listar_ofertas(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    ofertas = db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.estado == "pendiente",
    ).order_by(models.Oferta.precio).all()
    return [_enriquecer_oferta(db, o, incluir_fontanero=True) for o in ofertas]

@app.get("/fontaneros/{fontanero_id}/ofertas", response_model=List[schemas.OfertaRespuesta])
def listar_mis_ofertas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    ofertas = db.query(models.Oferta).filter(
        models.Oferta.fontanero_id == fontanero.id
    ).order_by(models.Oferta.id.desc()).all()
    return [_enriquecer_oferta(db, o, incluir_servicio=True) for o in ofertas]

@app.put("/servicios/{servicio_id}/ofertas/{oferta_id}/aceptar")
def aceptar_oferta(
    servicio_id: int,
    oferta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    oferta = db.query(models.Oferta).filter(
        models.Oferta.id == oferta_id,
        models.Oferta.servicio_id == servicio_id,
    ).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede aceptar ofertas de este servicio")
    servicio.fontanero_id = oferta.fontanero_id
    servicio.precio = oferta.precio
    servicio.estado = "aceptado"
    oferta.estado = "aceptada"
    db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.id != oferta_id,
    ).update({"estado": "rechazada"})
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == oferta.fontanero_id).first()
    if fontanero and fontanero.usuario_id:
        _crear_notificacion(db, fontanero.usuario_id, "¡Tu oferta fue aceptada!", f"El cliente aceptó tu oferta de {oferta.precio}€", "oferta_aceptada", servicio_id)
    db.commit()
    return {"mensaje": "Oferta aceptada"}

@app.put("/servicios/{servicio_id}/ofertas/{oferta_id}/rechazar")
def rechazar_oferta(
    servicio_id: int,
    oferta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    oferta = db.query(models.Oferta).filter(
        models.Oferta.id == oferta_id,
        models.Oferta.servicio_id == servicio_id,
    ).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede rechazar ofertas de este servicio")
    oferta.estado = "rechazada"
    db.commit()
    return {"mensaje": "Oferta rechazada"}

# ─── VALORACIÓN DETALLADA ─────────────────────────────────────────────────────

@app.post("/servicios/{servicio_id}/resena", response_model=schemas.ResenaRespuesta)
def crear_resena(
    servicio_id: int,
    datos: schemas.ResenaCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.estado != "pagado":
        raise HTTPException(status_code=400, detail="Solo se puede reseñar servicios pagados")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede reseñar")
    existente = db.query(models.Resena).filter(models.Resena.servicio_id == servicio_id).first()
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe una reseña para este servicio")
    media = round((datos.puntualidad + datos.calidad + datos.precio_justo + datos.trato) / 4, 2)
    resena = models.Resena(
        servicio_id=servicio_id,
        cliente_id=current_user["id"],
        fontanero_id=servicio.fontanero_id,
        puntualidad=datos.puntualidad,
        calidad=datos.calidad,
        precio_justo=datos.precio_justo,
        trato=datos.trato,
        comentario=datos.comentario,
    )
    db.add(resena)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
    if fontanero:
        resenas = db.query(models.Resena).filter(models.Resena.fontanero_id == fontanero.id).all()
        total = sum((r.puntualidad + r.calidad + r.precio_justo + r.trato) / 4 for r in resenas)
        fontanero.valoracion = round((total + media) / (len(resenas) + 1), 2)
        fontanero.num_trabajos = (fontanero.num_trabajos or 0) + 1
        if fontanero.usuario_id:
            _crear_notificacion(db, fontanero.usuario_id, "Nueva reseña recibida", f"Has recibido una valoración de {media}/5", "resena", servicio_id)
    db.commit()
    db.refresh(resena)
    return resena

@app.get("/fontaneros/{fontanero_id}/resenas", response_model=List[schemas.ResenaRespuesta])
def ver_resenas(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.Resena).filter(
        models.Resena.fontanero_id == fontanero.id
    ).order_by(models.Resena.creado_en.desc()).all()

@app.post("/servicios/{servicio_id}/resena-cliente", response_model=schemas.ResenaClienteRespuesta)
def crear_resena_cliente(
    servicio_id: int,
    datos: schemas.ResenaClienteCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.estado != "pagado":
        raise HTTPException(status_code=400, detail="Solo se puede reseñar servicios pagados")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="Solo el fontanero del servicio puede reseñar al cliente")
    existente = db.query(models.ResenaCliente).filter(models.ResenaCliente.servicio_id == servicio_id).first()
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe una reseña del cliente para este servicio")
    resena = models.ResenaCliente(
        servicio_id=servicio_id,
        fontanero_id=fontanero.id,
        cliente_id=servicio.cliente_id,
        puntualidad=datos.puntualidad,
        trato=datos.trato,
        comunicacion=datos.comunicacion,
        comentario=datos.comentario,
    )
    db.add(resena)
    db.commit()
    db.refresh(resena)
    return resena

# ─── CALENDARIO INTERNO ───────────────────────────────────────────────────────

@app.post("/fontaneros/{fontanero_id}/citas", response_model=schemas.CitaRespuesta)
def crear_cita(
    fontanero_id: int,
    datos: schemas.CitaCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    cita = models.Cita(
        fontanero_id=fontanero.id,
        servicio_id=datos.servicio_id,
        titulo=datos.titulo,
        fecha_inicio=datos.fecha_inicio,
        fecha_fin=datos.fecha_fin,
    )
    db.add(cita)
    db.commit()
    db.refresh(cita)
    return cita

@app.get("/fontaneros/{fontanero_id}/citas", response_model=List[schemas.CitaRespuesta])
def ver_citas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.Cita).filter(
        models.Cita.fontanero_id == fontanero.id
    ).order_by(models.Cita.fecha_inicio).all()

@app.delete("/fontaneros/{fontanero_id}/citas/{cita_id}")
def eliminar_cita(
    fontanero_id: int,
    cita_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    cita = db.query(models.Cita).filter(
        models.Cita.id == cita_id,
        models.Cita.fontanero_id == fontanero.id,
    ).first()
    if not cita:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    db.delete(cita)
    db.commit()
    return {"mensaje": "Cita eliminada"}

# ─── HISTORIAL DE PAGOS FONTANERO ─────────────────────────────────────────────

@app.get("/fontaneros/{fontanero_id}/pagos")
def historial_pagos(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    servicios = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "pagado",
        models.Servicio.precio != None,
    ).order_by(models.Servicio.creado_en.desc()).all()
    resultado = []
    for s in servicios:
        cliente = db.query(models.Usuario).filter(models.Usuario.id == s.cliente_id).first()
        comision = round(s.precio * 0.05, 2)
        resultado.append({
            "servicio_id": s.id,
            "tipo": s.tipo,
            "fecha": str(s.creado_en),
            "precio_bruto": s.precio,
            "comision_plataforma": comision,
            "precio_neto": round(s.precio - comision, 2),
            "metodo_pago": s.metodo_pago,
            "cliente": cliente.nombre if cliente else "Cliente",
        })
    return resultado

# ─── FACTURA PDF ──────────────────────────────────────────────────────────────

@app.get("/servicios/{servicio_id}/factura")
def descargar_factura(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    from fastapi.responses import Response
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        import io
    except ImportError:
        raise HTTPException(status_code=501, detail="Generación de PDF no disponible. Instala reportlab.")

    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.estado not in ["pagado", "pago_pendiente"]:
        raise HTTPException(status_code=400, detail="Solo se puede generar factura de servicios pagados")

    cliente = db.query(models.Usuario).filter(models.Usuario.id == servicio.cliente_id).first()
    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, h - 60, "Multiservicios Provenza - Factura")
    c.setFont("Helvetica", 12)
    c.drawString(50, h - 100, f"Nº Factura: {servicio_id:05d}")
    c.drawString(50, h - 120, f"Fecha: {servicio.creado_en.strftime('%d/%m/%Y') if servicio.creado_en else '-'}")
    c.drawString(50, h - 160, f"Cliente: {cliente.nombre if cliente else '-'}")
    c.drawString(50, h - 180, f"Email: {cliente.email if cliente else '-'}")
    c.drawString(50, h - 220, f"Profesional: {fontanero_obj.nombre if fontanero_obj else '-'}")
    c.drawString(50, h - 240, f"Zona: {fontanero_obj.zona if fontanero_obj else '-'}")
    c.drawString(50, h - 280, f"Servicio: {servicio.tipo}")
    c.drawString(50, h - 300, f"Descripción: {servicio.descripcion or '-'}")
    c.drawString(50, h - 340, f"Importe total: {servicio.precio or 0:.2f} EUR")
    c.drawString(50, h - 360, f"Método de pago: {servicio.metodo_pago or '-'}")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, 50, "Multiservicios Provenza - Plataforma de servicios del hogar")
    c.save()
    buf.seek(0)
    return Response(content=buf.read(), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=factura_{servicio_id}.pdf"})

# ─── STRIPE ───────────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")

@app.post("/servicios/{servicio_id}/stripe/crear-intent")
def crear_stripe_intent(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=501, detail="Stripe no configurado. Añade STRIPE_SECRET_KEY en variables de entorno.")
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
    except ImportError:
        raise HTTPException(status_code=501, detail="Librería stripe no instalada.")

    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio or not servicio.precio:
        raise HTTPException(status_code=400, detail="Servicio no encontrado o sin precio")
    intent = stripe.PaymentIntent.create(
        amount=int(servicio.precio * 100),
        currency="eur",
        metadata={"servicio_id": servicio_id},
    )
    servicio.stripe_payment_intent = intent.id
    db.commit()
    return {"client_secret": intent.client_secret, "amount": servicio.precio}

@app.post("/servicios/{servicio_id}/stripe/confirmar")
def confirmar_stripe(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.estado = "pagado"
    servicio.metodo_pago = "stripe"
    comision = round((servicio.precio or 0) * 0.05, 2)
    servicio.comision_aplicada = comision
    if servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Pago recibido por Stripe", f"Pago de {servicio.precio}€ confirmado", "pago_recibido", servicio_id)
    db.commit()
    return {"mensaje": "Pago confirmado", "precio": servicio.precio, "comision": comision}

@app.post("/servicios/{servicio_id}/stripe/crear-checkout")
def crear_stripe_checkout(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Pago real vía la página alojada de Stripe (Checkout): no requiere SDK nativo,
    así que funciona con Expo Go — el navegador se abre con expo-web-browser."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=501, detail="Stripe no configurado. Añade STRIPE_SECRET_KEY en variables de entorno.")
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
    except ImportError:
        raise HTTPException(status_code=501, detail="Librería stripe no instalada.")

    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio or not servicio.precio:
        raise HTTPException(status_code=400, detail="Servicio no encontrado o sin precio")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede pagar este servicio")

    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None
    comision_centavos = round(servicio.precio * 100 * 0.05)

    checkout_kwargs = dict(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "eur",
                "product_data": {"name": f"Multiservicios Provenza — {servicio.tipo}"},
                "unit_amount": int(servicio.precio * 100),
            },
            "quantity": 1,
        }],
        metadata={"servicio_id": str(servicio_id)},
        success_url=f"{os.getenv('BACKEND_URL', 'https://fontap-backend-production.up.railway.app')}/pago-resultado?estado=ok",
        cancel_url=f"{os.getenv('BACKEND_URL', 'https://fontap-backend-production.up.railway.app')}/pago-resultado?estado=cancelado",
    )
    # Si el fontanero ya conectó su cuenta de Stripe, repartimos automático:
    # la comisión se queda en Multiservicios Provenza, el resto va directo a su cuenta.
    if fontanero_obj and fontanero_obj.stripe_account_id:
        checkout_kwargs["payment_intent_data"] = {
            "application_fee_amount": comision_centavos,
            "transfer_data": {"destination": fontanero_obj.stripe_account_id},
        }
    session = stripe.checkout.Session.create(**checkout_kwargs)
    servicio.stripe_payment_intent = session.id
    db.commit()
    return {"checkout_url": session.url, "session_id": session.id}

@app.post("/servicios/{servicio_id}/stripe/verificar")
def verificar_stripe_checkout(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El frontend llama esto al volver del navegador de pago; verificamos directo
    con Stripe (nunca confiamos en que el cliente 'diga' que pagó)."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=501, detail="Stripe no configurado.")
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio or not servicio.stripe_payment_intent:
        raise HTTPException(status_code=400, detail="No hay sesión de pago para este servicio")

    session = stripe.checkout.Session.retrieve(servicio.stripe_payment_intent)
    ya_pagado = servicio.estado == "pagado"
    if session.payment_status == "paid" and not ya_pagado:
        servicio.estado = "pagado"
        servicio.metodo_pago = "stripe"
        comision = round((servicio.precio or 0) * 0.05, 2)
        servicio.comision_aplicada = comision
        servicio.comision_liquidada = True  # Stripe ya retuvo/repartió la comisión al cobrar
        if servicio.fontanero_id:
            fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
            if fontanero_obj and fontanero_obj.usuario_id:
                _crear_notificacion(db, fontanero_obj.usuario_id, "Pago recibido", f"Pago de {servicio.precio}€ confirmado por Stripe", "pago_recibido", servicio_id)
        db.commit()
    return {"pagado": session.payment_status == "paid"}

@app.get("/pago-resultado")
def pago_resultado(estado: str = "ok"):
    from fastapi.responses import HTMLResponse
    if estado == "ok":
        titulo, texto = "✅ Pago completado", "Ya puedes cerrar esta ventana y volver a la app Multiservicios Provenza."
    else:
        titulo, texto = "Pago cancelado", "Puedes volver a la app Multiservicios Provenza e intentarlo de nuevo."
    html = f"""<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>body{{font-family:-apple-system,sans-serif;text-align:center;padding:60px 24px;background:#0A1A2A;color:#fff}}
    h1{{font-size:22px}} p{{color:#9AA6B8}}</style></head>
    <body><h1>{titulo}</h1><p>{texto}</p></body></html>"""
    return HTMLResponse(html)

# ─── STRIPE CONNECT (cobro automático para el fontanero) ─────────────────────

def _stripe_o_501():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=501, detail="Stripe no configurado. Añade STRIPE_SECRET_KEY en variables de entorno.")
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe

@app.post("/fontaneros/{fontanero_id}/stripe/conectar")
def conectar_stripe_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Genera (o reanuda) el onboarding de Stripe Connect Express para que el
    fontanero reciba sus cobros directo a su cuenta bancaria."""
    stripe = _stripe_o_501()
    if fontanero_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el propio fontanero puede conectar su cuenta")
    fontanero = get_or_create_fontanero(db, fontanero_id)
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    if not fontanero.stripe_account_id:
        cuenta = stripe.Account.create(
            type="express",
            email=current_user.get("sub"),
            capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
        )
        fontanero.stripe_account_id = cuenta.id
        db.commit()

    base_url = os.getenv("BACKEND_URL", "https://fontap-backend-production.up.railway.app")
    link = stripe.AccountLink.create(
        account=fontanero.stripe_account_id,
        refresh_url=f"{base_url}/pago-resultado?estado=cancelado",
        return_url=f"{base_url}/pago-resultado?estado=ok",
        type="account_onboarding",
    )
    return {"onboarding_url": link.url}

@app.get("/fontaneros/{fontanero_id}/stripe/estado")
def estado_stripe_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if not fontanero.stripe_account_id:
        return {"conectado": False, "cobros_activos": False}
    stripe = _stripe_o_501()
    cuenta = stripe.Account.retrieve(fontanero.stripe_account_id)
    return {"conectado": True, "cobros_activos": bool(cuenta.charges_enabled)}

# ─── COMISIÓN PENDIENTE (pagos en efectivo/Bizum que no pasan por Stripe) ─────

@app.get("/fontaneros/{fontanero_id}/comision-pendiente")
def comision_pendiente(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if fontanero_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="No puedes ver la comisión de otro fontanero")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    pendientes = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.comision_liquidada == False,
        models.Servicio.comision_aplicada != None,
    ).all()
    total = round(sum(s.comision_aplicada for s in pendientes), 2)
    return {"total": total, "servicios": [s.id for s in pendientes]}

@app.post("/fontaneros/{fontanero_id}/comision-pendiente/pagar")
def pagar_comision_pendiente(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    stripe = _stripe_o_501()
    if fontanero_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="No puedes pagar la comisión de otro fontanero")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    pendientes = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.comision_liquidada == False,
        models.Servicio.comision_aplicada != None,
    ).all()
    total = round(sum(s.comision_aplicada for s in pendientes), 2)
    if total <= 0:
        raise HTTPException(status_code=400, detail="No tienes comisión pendiente")

    base_url = os.getenv("BACKEND_URL", "https://fontap-backend-production.up.railway.app")
    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "eur",
                "product_data": {"name": "Multiservicios Provenza — comisión pendiente"},
                "unit_amount": round(total * 100),
            },
            "quantity": 1,
        }],
        metadata={"fontanero_id": str(fontanero_id), "tipo": "comision_pendiente"},
        success_url=f"{base_url}/pago-resultado?estado=ok",
        cancel_url=f"{base_url}/pago-resultado?estado=cancelado",
    )
    fontanero.comision_checkout_session = session.id
    db.commit()
    return {"checkout_url": session.url, "session_id": session.id, "total": total}

@app.post("/fontaneros/{fontanero_id}/comision-pendiente/verificar")
def verificar_comision_pendiente(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    stripe = _stripe_o_501()
    if fontanero_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="No puedes verificar la comisión de otro fontanero")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if not fontanero.comision_checkout_session:
        raise HTTPException(status_code=400, detail="No hay un pago de comisión en curso")
    session = stripe.checkout.Session.retrieve(fontanero.comision_checkout_session)
    if session.payment_status == "paid":
        db.query(models.Servicio).filter(
            models.Servicio.fontanero_id == fontanero.id,
            models.Servicio.comision_liquidada == False,
        ).update({"comision_liquidada": True}, synchronize_session=False)
        fontanero.comision_checkout_session = None
        db.commit()
    return {"liquidada": session.payment_status == "paid"}

# ─── BIZUM ────────────────────────────────────────────────────────────────────

@app.get("/servicios/{servicio_id}/bizum/instrucciones")
def instrucciones_bizum(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio or not servicio.precio:
        raise HTTPException(status_code=400, detail="Servicio no encontrado o sin precio")
    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None
    usuario_fontanero = db.query(models.Usuario).filter(models.Usuario.id == fontanero_obj.usuario_id).first() if fontanero_obj else None
    return {
        "importe": servicio.precio,
        "concepto": f"Multiservicios Provenza servicio #{servicio_id}",
        "telefono_destino": usuario_fontanero.telefono if usuario_fontanero else "Consultar con el profesional",
        "instrucciones": [
            f"1. Abre tu app bancaria y selecciona Bizum",
            f"2. Envía {servicio.precio}€ al teléfono del profesional",
            f"3. Añade el concepto: Multiservicios Provenza servicio #{servicio_id}",
            f"4. Vuelve a la app y confirma el pago",
        ],
    }

# ─── INMUEBLES (ADMIN FINCAS) ─────────────────────────────────────────────────

@app.post("/inmuebles", response_model=schemas.InmuebleRespuesta)
def crear_inmueble(
    datos: schemas.InmuebleCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    inmueble = models.Inmueble(
        administrador_id=current_user["id"],
        nombre=datos.nombre,
        direccion=datos.direccion,
        ciudad=datos.ciudad,
    )
    db.add(inmueble)
    db.commit()
    db.refresh(inmueble)
    return inmueble

@app.get("/inmuebles", response_model=List[schemas.InmuebleRespuesta])
def listar_inmuebles(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.Inmueble).filter(
        models.Inmueble.administrador_id == current_user["id"]
    ).all()

@app.delete("/inmuebles/{inmueble_id}")
def eliminar_inmueble(
    inmueble_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    inmueble = db.query(models.Inmueble).filter(
        models.Inmueble.id == inmueble_id,
        models.Inmueble.administrador_id == current_user["id"],
    ).first()
    if not inmueble:
        raise HTTPException(status_code=404, detail="Inmueble no encontrado")
    db.delete(inmueble)
    db.commit()
    return {"mensaje": "Inmueble eliminado"}

# ─── VERIFICACIÓN DE IDENTIDAD ────────────────────────────────────────────────

@app.post("/fontaneros/{fontanero_id}/documentos", response_model=schemas.DocumentoRespuesta)
def subir_documento(
    fontanero_id: int,
    tipo: str,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "pdf"
    nombre_archivo = f"doc_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    doc = models.DocumentoVerificacion(
        fontanero_id=fontanero.id,
        tipo=tipo,
        url=f"/uploads/{nombre_archivo}",
        estado="pendiente",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc

@app.get("/fontaneros/{fontanero_id}/documentos", response_model=List[schemas.DocumentoRespuesta])
def ver_documentos(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.DocumentoVerificacion).filter(
        models.DocumentoVerificacion.fontanero_id == fontanero.id
    ).all()

# ─── PANEL DE ADMINISTRACIÓN ──────────────────────────────────────────────────

@app.get("/admin", include_in_schema=False)
def panel_admin():
    from fastapi.responses import HTMLResponse
    ruta = os.path.join(os.path.dirname(__file__), "static", "admin.html")
    with open(ruta, encoding="utf-8") as f:
        return HTMLResponse(f.read())

def _verificar_admin(current_user: dict):
    if current_user.get("tipo") not in ["admin"]:
        raise HTTPException(status_code=403, detail="Acceso solo para administradores")

@app.get("/admin/estadisticas", response_model=schemas.AdminStats)
def admin_estadisticas(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    total_usuarios = db.query(models.Usuario).count()
    total_fontaneros = db.query(models.Usuario).filter(models.Usuario.tipo == "fontanero").count()
    total_clientes = db.query(models.Usuario).filter(models.Usuario.tipo == "cliente").count()
    total_servicios = db.query(models.Servicio).count()
    servicios_pendientes = db.query(models.Servicio).filter(models.Servicio.estado == "pendiente").count()
    servicios_completados = db.query(models.Servicio).filter(models.Servicio.estado == "pagado").count()
    pagados = db.query(models.Servicio).filter(
        models.Servicio.estado == "pagado",
        models.Servicio.precio != None,
    ).all()
    ingresos = sum(s.precio * 0.05 for s in pagados if s.precio)
    dinero_movido = sum(s.precio for s in pagados if s.precio)
    comisiones_pendientes = sum(
        (s.comision_aplicada or 0) for s in db.query(models.Servicio).filter(
            models.Servicio.comision_liquidada == False,
        ).all()
    )
    hace_7d = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    usuarios_nuevos_7d = db.query(models.Usuario).filter(models.Usuario.creado_en >= hace_7d).count()
    servicios_7d = db.query(models.Servicio).filter(models.Servicio.creado_en >= hace_7d).count()
    usuarios_bloqueados = db.query(models.Usuario).filter(models.Usuario.bloqueado == True).count()
    por_gremio = {}
    for f in db.query(models.Fontanero).all():
        g = f.gremio or "fontanero"
        por_gremio[g] = por_gremio.get(g, 0) + 1
    return {
        "total_usuarios": total_usuarios,
        "total_fontaneros": total_fontaneros,
        "total_clientes": total_clientes,
        "total_servicios": total_servicios,
        "servicios_pendientes": servicios_pendientes,
        "servicios_completados": servicios_completados,
        "ingresos_plataforma": round(ingresos, 2),
        "dinero_movido": round(dinero_movido, 2),
        "comisiones_pendientes": round(comisiones_pendientes, 2),
        "usuarios_nuevos_7d": usuarios_nuevos_7d,
        "servicios_7d": servicios_7d,
        "usuarios_bloqueados": usuarios_bloqueados,
        "por_gremio": por_gremio,
    }

@app.get("/admin/usuarios", response_model=List[schemas.UsuarioRespuesta])
def admin_listar_usuarios(
    tipo: Optional[str] = None,
    buscar: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    q = db.query(models.Usuario)
    if tipo:
        q = q.filter(models.Usuario.tipo == tipo)
    if buscar:
        patron = f"%{buscar}%"
        q = q.filter(or_(models.Usuario.nombre.ilike(patron), models.Usuario.email.ilike(patron)))
    return q.order_by(models.Usuario.creado_en.desc()).limit(200).all()

class BloquearDatos(BaseModel):
    bloqueado: bool

@app.put("/admin/usuarios/{usuario_id}/bloquear")
def admin_bloquear_usuario(
    usuario_id: int,
    datos: BloquearDatos,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if usuario.tipo == "admin":
        raise HTTPException(status_code=400, detail="No puedes vetar a otro administrador")
    usuario.bloqueado = datos.bloqueado
    # Si es profesional, al vetarlo desaparece del mapa; al desvetarlo debe reactivarse él mismo
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario_id).first()
    if fontanero and datos.bloqueado:
        fontanero.disponible = False
    db.commit()
    return {"mensaje": "Usuario vetado" if datos.bloqueado else "Usuario desvetado", "bloqueado": usuario.bloqueado}

@app.get("/admin/fontaneros")
def admin_listar_fontaneros(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    resultado = []
    for f in db.query(models.Fontanero).all():
        usuario = db.query(models.Usuario).filter(models.Usuario.id == f.usuario_id).first() if f.usuario_id else None
        comision_pendiente = sum(
            (s.comision_aplicada or 0) for s in db.query(models.Servicio).filter(
                models.Servicio.fontanero_id == f.id,
                models.Servicio.comision_liquidada == False,
            ).all()
        )
        resultado.append({
            "id": f.id,
            "usuario_id": f.usuario_id,
            "nombre": f.nombre,
            "email": usuario.email if usuario else None,
            "gremio": f.gremio or "fontanero",
            "verificado": f.verificado,
            "disponible": f.disponible,
            "valoracion": f.valoracion,
            "num_trabajos": f.num_trabajos or 0,
            "stripe_conectado": bool(f.stripe_account_id),
            "comision_pendiente": round(comision_pendiente, 2),
            "bloqueado": usuario.bloqueado if usuario else False,
        })
    return resultado

@app.get("/admin/servicios")
def admin_listar_servicios(
    estado: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    q = db.query(models.Servicio)
    if estado:
        q = q.filter(models.Servicio.estado == estado)
    return q.order_by(models.Servicio.creado_en.desc()).all()

@app.put("/admin/fontaneros/{fontanero_id}/verificar")
def admin_verificar_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.verificado = True
    if fontanero.usuario_id:
        _crear_notificacion(db, fontanero.usuario_id, "¡Perfil verificado!", "Tu perfil ha sido verificado por Multiservicios Provenza", "verificacion", fontanero_id)
    db.commit()
    return {"mensaje": "Fontanero verificado"}

@app.put("/admin/documentos/{doc_id}/revisar")
def admin_revisar_documento(
    doc_id: int,
    estado: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    if estado not in ["verificado", "rechazado"]:
        raise HTTPException(status_code=400, detail="Estado debe ser verificado o rechazado")
    doc = db.query(models.DocumentoVerificacion).filter(models.DocumentoVerificacion.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    doc.estado = estado
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == doc.fontanero_id).first()
    if fontanero and fontanero.usuario_id:
        msg = "aprobado" if estado == "verificado" else "rechazado"
        _crear_notificacion(db, fontanero.usuario_id, f"Documento {msg}", f"Tu documento '{doc.tipo}' ha sido {msg}", "documento", doc_id)
    db.commit()
    return {"mensaje": f"Documento {estado}"}

# ─── MENSAJES CHAT ────────────────────────────────────────────────────────────

class MensajeCrear(BaseModel):
    contenido: str

@app.post("/servicios/{servicio_id}/mensajes")
def enviar_mensaje(
    servicio_id: int,
    mensaje: MensajeCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    emisor_id = current_user["id"]
    emisor = db.query(models.Usuario).filter(models.Usuario.id == emisor_id).first()
    nuevo = models.Mensaje(
        servicio_id=servicio_id,
        emisor_id=emisor_id,
        texto=mensaje.contenido,
        leido=False,
    )
    db.add(nuevo)
    if emisor_id == servicio.cliente_id and servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Nuevo mensaje", mensaje.contenido[:80], "mensaje", servicio_id)
    elif servicio.cliente_id != emisor_id:
        _crear_notificacion(db, servicio.cliente_id, "Nuevo mensaje", mensaje.contenido[:80], "mensaje", servicio_id)
    db.commit()
    db.refresh(nuevo)
    return {
        "id": nuevo.id,
        "contenido": nuevo.texto,
        "remitente_tipo": emisor.tipo if emisor else "cliente",
        "remitente_nombre": emisor.nombre if emisor else "Usuario",
        "creado_en": str(nuevo.creado_en),
    }

@app.get("/servicios/{servicio_id}/mensajes")
def ver_mensajes(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    mensajes = db.query(models.Mensaje).filter(
        models.Mensaje.servicio_id == servicio_id
    ).order_by(models.Mensaje.creado_en).all()
    resultado = []
    for m in mensajes:
        emisor = db.query(models.Usuario).filter(models.Usuario.id == m.emisor_id).first()
        resultado.append({
            "id": m.id,
            "contenido": m.texto,
            "remitente_tipo": emisor.tipo if emisor else "cliente",
            "remitente_nombre": emisor.nombre if emisor else "Usuario",
            "creado_en": str(m.creado_en),
        })
    return resultado
# ─── CHAT GENERAL ─────────────────────────────────────────────────────────────

class MensajeChatCrear(BaseModel):
    contenido: str
    remitente_tipo: str
    remitente_nombre: Optional[str] = None

def _escapar_like(valor: str) -> str:
    return valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

@app.post("/chat/{chat_id}/mensajes")
def enviar_mensaje_chat(
    chat_id: str,
    mensaje: MensajeChatCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    nombre = mensaje.remitente_nombre or "Usuario"
    nuevo = models.Mensaje(
        servicio_id=0,
        emisor_id=current_user["id"],
        texto=f"{chat_id}||{mensaje.remitente_tipo}||{nombre}||{mensaje.contenido}",
        leido=False,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return {
        "id": nuevo.id,
        "contenido": mensaje.contenido,
        "remitente_tipo": mensaje.remitente_tipo,
        "remitente_nombre": nombre,
        "creado_en": str(nuevo.creado_en),
    }

@app.get("/chat/{chat_id}/mensajes")
def ver_mensajes_chat(
    chat_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    mensajes = db.query(models.Mensaje).filter(
        models.Mensaje.texto.like(f"{_escapar_like(chat_id)}||%", escape="\\")
    ).order_by(models.Mensaje.creado_en).all()
    resultado = []
    for m in mensajes:
        partes = m.texto.split("||", 3)
        if len(partes) == 4:
            resultado.append({
                "id": m.id,
                "contenido": partes[3],
                "remitente_tipo": partes[1],
                "remitente_nombre": partes[2],
                "creado_en": str(m.creado_en),
            })
    return resultado
# ─── RECORDATORIOS DE CITAS ────────────────────────────────────────────────────
# Hilo en segundo plano: cada 5 minutos revisa las citas próximas y avisa
# (push + notificación in-app) 24h y 1h antes, al profesional y al cliente.

def _avisar_cita(db, cita, cuando: str):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == cita.fontanero_id).first()
    hora = cita.fecha_inicio.strftime("%H:%M") if cita.fecha_inicio else ""
    titulo = f"📅 Recordatorio: cita {cuando}"
    if fontanero and fontanero.usuario_id:
        _crear_notificacion(db, fontanero.usuario_id, titulo,
                            f"{cita.titulo or 'Cita'} a las {hora}", "recordatorio", cita.id)
    if cita.servicio_id:
        servicio = db.query(models.Servicio).filter(models.Servicio.id == cita.servicio_id).first()
        if servicio and servicio.estado not in ["cancelado", "rechazado"]:
            nombre_prof = fontanero.nombre if fontanero else "tu profesional"
            _crear_notificacion(db, servicio.cliente_id, titulo,
                                f"Tu cita con {nombre_prof} es {cuando} a las {hora}", "recordatorio", cita.servicio_id)

def _bucle_recordatorios():
    from .database import SessionLocal
    while True:
        db = None
        try:
            db = SessionLocal()
            ahora = datetime.datetime.utcnow()
            proximas = db.query(models.Cita).filter(
                models.Cita.fecha_inicio != None,
                models.Cita.fecha_inicio > ahora,
            ).all()
            for cita in proximas:
                segundos = (cita.fecha_inicio - ahora).total_seconds()
                if segundos <= 3600 and not cita.recordatorio_1h:
                    _avisar_cita(db, cita, "en menos de 1 hora")
                    cita.recordatorio_1h = True
                    cita.recordatorio_24h = True
                elif segundos <= 86400 and not cita.recordatorio_24h:
                    _avisar_cita(db, cita, "mañana")
                    cita.recordatorio_24h = True
            db.commit()
        except Exception as e:
            print(f"[recordatorios] error: {e}")
        finally:
            if db is not None:
                db.close()
        _time.sleep(300)

if os.getenv("DESACTIVAR_RECORDATORIOS", "") != "1":
    threading.Thread(target=_bucle_recordatorios, daemon=True).start()
