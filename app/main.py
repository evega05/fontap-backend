from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_, nullslast, inspect, text, func
from typing import List, Literal, Optional
from pydantic import BaseModel, Field
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

# Como Habitissimo: máximo de profesionales que pueden ofertar por un mismo
# servicio abierto, para no saturar al cliente con demasiadas propuestas.
CUPO_MAXIMO_OFERTAS = 4

def _norm_email(email: str) -> str:
    """Un email dado por el usuario (registro, login, reset...) se compara siempre
    en minúsculas y sin espacios, para que un teclado que capitaliza la primera
    letra o un espacio de más no rompa el login/registro."""
    return (email or "").strip().lower()

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Muchos hosts (Railway incluido) bloquean las conexiones salientes por el
# protocolo SMTP para evitar spam, aunque el HTTPS normal funcione sin problema
# (confirmado: SMTP da "Network is unreachable", un curl a HTTPS responde 200).
# Por eso el envío real va por la API HTTP de Brevo (no requiere dominio propio,
# solo verificar una dirección de remitente); SMTP se deja como reserva única-
# mente para entornos (locales, otros hostings) donde sí esté disponible.
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_FROM_EMAIL = os.getenv("BREVO_FROM_EMAIL", "")
BREVO_FROM_NOMBRE = os.getenv("BREVO_FROM_NOMBRE", "Multiservicios Provenza")

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
            "descripcion": "TEXT",
            "especialidades": "TEXT",
            "vacaciones_desde": "TIMESTAMP",
            "vacaciones_hasta": "TIMESTAMP",
            "gremio": "VARCHAR DEFAULT 'fontanero'",
            "verificado": "BOOLEAN DEFAULT FALSE",
            "num_trabajos": "INTEGER DEFAULT 0",
            "certificado_pro": "BOOLEAN DEFAULT FALSE",
            "codigo_referido": "VARCHAR",
            "referido_por_id": "INTEGER",
            "referido_hasta": "TIMESTAMP",
            "primeros_trabajos_gratis": "INTEGER DEFAULT 3",
            "google_calendar_refresh_token": "VARCHAR",
            "google_calendar_conectado": "BOOLEAN DEFAULT FALSE",
            "nombre_empresa": "VARCHAR",
            "empresa_id": "INTEGER",
        },
        "usuarios": {
            "terminos_aceptados": "BOOLEAN DEFAULT FALSE",
            "email_verificado": "BOOLEAN DEFAULT FALSE",
            "bloqueado": "BOOLEAN DEFAULT FALSE",
        },
        "servicios": {
            "comision_liquidada": "BOOLEAN DEFAULT TRUE",
            "gremio": "VARCHAR",
            "google_event_id": "VARCHAR",
            "ciudad": "VARCHAR",
            "radio_ampliado": "BOOLEAN DEFAULT FALSE",
            "latitud_cliente": "FLOAT",
            "longitud_cliente": "FLOAT",
            "aviso_proximidad_enviado": "BOOLEAN DEFAULT FALSE",
            "es_consulta": "BOOLEAN DEFAULT FALSE",
            "fontanero_preferente_id": "INTEGER",
            "prioridad_hasta": "TIMESTAMP",
        },
        "favoritos": {
            "preferente": "BOOLEAN DEFAULT FALSE",
        },
        "citas": {
            "recordatorio_24h": "BOOLEAN DEFAULT FALSE",
            "recordatorio_1h": "BOOLEAN DEFAULT FALSE",
        },
        "ofertas": {
            "materiales": "FLOAT",
            "mano_obra": "FLOAT",
        },
        "mensajes": {
            "imagen_url": "VARCHAR",
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
    except Exception as e:
        print(f"[limpiar-valoraciones] Error: {e}")
        db.rollback()
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
    # La API solo usa Bearer tokens en el header Authorization (no cookies), así que
    # allow_credentials no aporta nada aquí. Con allow_origins=["*"], dejarlo en True
    # hace que el middleware refleje el Origin de la request en vez de mandar "*",
    # lo que permite peticiones "credenciadas" desde cualquier origen sin necesidad.
    allow_credentials=False,
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

COMISION_ESTANDAR = 0.05
COMISION_REDUCIDA_REFERIDO = 0.025
LIMITE_SERVICIOS_COMISION_PENDIENTE = 10

def _tasa_comision(fontanero) -> float:
    """0% mientras le queden trabajos gratis de bienvenida; 2.5% si está dentro
    del periodo de comisión reducida por haber invitado a un colega de su gremio
    (programa 'trae a tu gremio'); 5% en el resto de casos."""
    if fontanero and (fontanero.primeros_trabajos_gratis or 0) > 0:
        return 0.0
    if fontanero and fontanero.referido_hasta and fontanero.referido_hasta > datetime.datetime.utcnow():
        return COMISION_REDUCIDA_REFERIDO
    return COMISION_ESTANDAR

def _aplicar_comision_atomica(db: Session, fontanero) -> float:
    """Calcula la comisión y consume el trabajo gratis (si aplica) en un único
    UPDATE...WHERE atómico: leer primeros_trabajos_gratis y decrementarlo por
    separado permitía que dos confirmaciones de pago concurrentes del mismo
    fontanero (en dos servicios distintos) leyeran el mismo contador antes de
    que ninguna escribiera, y las dos se llevaran 0% de comisión consumiendo
    en la práctica una sola unidad del cupo de bienvenida (lost update, mismo
    patrón que ya se corrigió en aceptar_servicio/aceptar_oferta)."""
    if fontanero:
        filas = db.query(models.Fontanero).filter(
            models.Fontanero.id == fontanero.id,
            models.Fontanero.primeros_trabajos_gratis > 0,
        ).update(
            {"primeros_trabajos_gratis": models.Fontanero.primeros_trabajos_gratis - 1},
            synchronize_session=False,
        )
        if filas > 0:
            return 0.0
        # synchronize_session=False no refresca el atributo en memoria: sin este
        # refresh, _tasa_comision volvería a leer el primeros_trabajos_gratis
        # que este objeto tenía ANTES del intento de UPDATE (que puede seguir
        # marcando >0 aunque el UPDATE ya haya determinado, contra el valor real
        # en la base de datos, que no quedaba ninguno) y devolvería 0% igualmente.
        db.refresh(fontanero)
    return _tasa_comision(fontanero)

def _num_servicios_comision_pendiente(db: Session, fontanero) -> int:
    return db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.comision_liquidada == False,
        models.Servicio.comision_aplicada != None,
    ).count()

def _verificar_email_profesional(db: Session, usuario_id: int):
    """Impide que un profesional reciba/acepte trabajos hasta que confirme su email,
    para que no se pueda operar como profesional con un email inventado."""
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if usuario and not usuario.email_verificado:
        raise HTTPException(
            status_code=403,
            detail="Debes verificar tu email antes de recibir o aceptar trabajos. Revisa tu bandeja de entrada o reenvía el código desde Ajustes.",
        )

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

def _registrar_auditoria(db: Session, servicio_id: int, campo: str, anterior, nuevo, current_user: dict = None):
    db.add(models.AuditoriaServicio(
        servicio_id=servicio_id,
        campo=campo,
        valor_anterior=str(anterior) if anterior is not None else None,
        valor_nuevo=str(nuevo) if nuevo is not None else None,
        cambiado_por_id=current_user["id"] if current_user else None,
        cambiado_por_tipo=current_user.get("tipo") if current_user else "sistema",
    ))

UMBRAL_CANCELACIONES = 3
DIAS_VENTANA_CANCELACIONES = 30
UMBRAL_RESENAS_BAJAS_MEDIA = 2.5
MINIMO_RESENAS_PARA_ALERTA = 3

def _revisar_cancelaciones_repetidas(db: Session, fontanero_id: int):
    if not fontanero_id:
        return
    desde = datetime.datetime.utcnow() - datetime.timedelta(days=DIAS_VENTANA_CANCELACIONES)
    total = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero_id,
        models.Servicio.estado == "cancelado",
        models.Servicio.creado_en >= desde,
    ).count()
    if total < UMBRAL_CANCELACIONES:
        return
    ya_avisado = db.query(models.AlertaAdmin).filter(
        models.AlertaAdmin.tipo == "cancelaciones_repetidas",
        models.AlertaAdmin.fontanero_id == fontanero_id,
        models.AlertaAdmin.creado_en >= desde,
    ).first()
    if ya_avisado:
        return
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    db.add(models.AlertaAdmin(
        tipo="cancelaciones_repetidas",
        fontanero_id=fontanero_id,
        mensaje=f"{fontanero.nombre if fontanero else 'Un profesional'} acumula {total} cancelaciones en los últimos {DIAS_VENTANA_CANCELACIONES} días",
    ))

def _revisar_resenas_bajas(db: Session, fontanero_id: int):
    if not fontanero_id:
        return
    ultimas = db.query(models.Resena).filter(
        models.Resena.fontanero_id == fontanero_id
    ).order_by(models.Resena.id.desc()).limit(MINIMO_RESENAS_PARA_ALERTA).all()
    if len(ultimas) < MINIMO_RESENAS_PARA_ALERTA:
        return
    media = sum((r.puntualidad + r.calidad + r.precio_justo + r.trato) / 4 for r in ultimas) / len(ultimas)
    if media > UMBRAL_RESENAS_BAJAS_MEDIA:
        return
    desde = datetime.datetime.utcnow() - datetime.timedelta(days=DIAS_VENTANA_CANCELACIONES)
    ya_avisado = db.query(models.AlertaAdmin).filter(
        models.AlertaAdmin.tipo == "resenas_bajas",
        models.AlertaAdmin.fontanero_id == fontanero_id,
        models.AlertaAdmin.creado_en >= desde,
    ).first()
    if ya_avisado:
        return
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    db.add(models.AlertaAdmin(
        tipo="resenas_bajas",
        fontanero_id=fontanero_id,
        mensaje=f"{fontanero.nombre if fontanero else 'Un profesional'} promedia {round(media, 2)}/5 en sus últimas {len(ultimas)} reseñas",
    ))

# ─── AUTH ──────────────────────────────────────────────────────────────────────

@app.get("/")
def inicio():
    return {"mensaje": "Multiservicios Provenza API funcionando"}

# ─── LANDING PAGES SEO (gremio × ciudad) ───────────────────────────────────────
GREMIOS_LANDING = {
    "fontanero": {"nombre": "Fontanero", "plural": "fontaneros", "emoji": "🔧", "servicios": ["Desatascos", "Fugas de agua", "Calderas", "Grifería y duchas"]},
    "electricista": {"nombre": "Electricista", "plural": "electricistas", "emoji": "⚡", "servicios": ["Cortocircuitos", "Instalaciones eléctricas", "Cuadros eléctricos", "Iluminación"]},
    "cerrajero": {"nombre": "Cerrajero", "plural": "cerrajeros", "emoji": "🔑", "servicios": ["Apertura de puertas", "Cambio de cerraduras", "Llaves perdidas", "Puertas blindadas"]},
    "pintor": {"nombre": "Pintor", "plural": "pintores", "emoji": "🎨", "servicios": ["Pintura de interior", "Pintura de fachada", "Gotelé y alisado", "Humedades"]},
    "carpintero": {"nombre": "Carpintero", "plural": "carpinteros", "emoji": "🪚", "servicios": ["Muebles a medida", "Puertas y armarios", "Suelos de madera", "Reparación de muebles"]},
    "albanil": {"nombre": "Albañil", "plural": "albañiles", "emoji": "🧱", "servicios": ["Reformas", "Alicatados", "Grietas y humedades", "Tabiquería"]},
    "climatizacion": {"nombre": "Técnico de climatización", "plural": "técnicos de climatización", "emoji": "❄️", "servicios": ["Instalación de aire acondicionado", "Mantenimiento de calderas", "Averías de climatización", "Bombas de calor"]},
    "jardinero": {"nombre": "Jardinero", "plural": "jardineros", "emoji": "🌿", "servicios": ["Mantenimiento de jardines", "Poda", "Diseño de jardines", "Riego automático"]},
    "limpieza": {"nombre": "Servicio de limpieza", "plural": "profesionales de limpieza", "emoji": "🧹", "servicios": ["Limpieza de hogar", "Limpieza tras obra", "Limpieza de oficinas", "Limpieza de cristales"]},
    "mudanzas": {"nombre": "Empresa de mudanzas", "plural": "profesionales de mudanzas", "emoji": "📦", "servicios": ["Mudanzas locales", "Guardamuebles", "Embalaje", "Montaje de muebles"]},
    "montador": {"nombre": "Montador de muebles", "plural": "montadores de muebles", "emoji": "🪑", "servicios": ["Montaje de IKEA", "Montaje de cocinas", "Colgado de estanterías", "Ensamblaje de muebles"]},
    "cristalero": {"nombre": "Cristalero", "plural": "cristaleros", "emoji": "🪟", "servicios": ["Cambio de cristales", "Mamparas de ducha", "Cristales de seguridad", "Escaparates"]},
}
CIUDADES_LANDING = {"bilbao": "Bilbao", "madrid": "Madrid", "barcelona": "Barcelona"}


def _landing_base_url() -> str:
    return os.getenv("BACKEND_URL", "https://fontap-backend-production.up.railway.app")


def _landing_head(titulo: str, descripcion: str, canonical: str, json_ld: dict) -> str:
    import json as _json
    return f"""<meta charset="utf-8">
<title>{titulo}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{descripcion}">
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{titulo}">
<meta property="og:description" content="{descripcion}">
<meta property="og:type" content="website">
<meta name="robots" content="index, follow">
<script type="application/ld+json">{_json.dumps(json_ld, ensure_ascii=False)}</script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 720px; margin: 0 auto; padding: 40px 20px; color: #17202a; line-height: 1.6; }}
  h1 {{ font-size: 28px; margin-bottom: 8px; }}
  h2 {{ font-size: 19px; margin-top: 32px; }}
  .emoji {{ font-size: 40px; }}
  ul {{ padding-left: 22px; }}
  li {{ margin-bottom: 4px; }}
  .cta {{ margin-top: 32px; padding: 20px; background: #f2f4f7; border-radius: 14px; text-align: center; }}
  a {{ color: #276EF1; }}
  footer {{ margin-top: 48px; font-size: 12.5px; color: #8a94a6; }}
</style>"""


def _landing_html(gremio: str, ciudad: str) -> str:
    info = GREMIOS_LANDING[gremio]
    ciudad_nombre = CIUDADES_LANDING[ciudad]
    canonical = f"{_landing_base_url()}/landing/{gremio}/{ciudad}"
    titulo = f"{info['nombre']} en {ciudad_nombre} | Multiservicios Provenza"
    descripcion = f"Encuentra {info['plural']} verificados en {ciudad_nombre}. Presupuesto claro antes de empezar, pago seguro y reseñas reales. {', '.join(info['servicios'])}."
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Service",
        "serviceType": info["nombre"],
        "areaServed": {"@type": "City", "name": ciudad_nombre},
        "provider": {"@type": "Organization", "name": "Multiservicios Provenza"},
        "description": descripcion,
    }
    servicios_li = "".join(f"<li>{s}</li>" for s in info["servicios"])
    otros_gremios = " · ".join(
        f'<a href="/landing/{g}/{ciudad}">{i["emoji"]} {i["nombre"]}</a>'
        for g, i in GREMIOS_LANDING.items() if g != gremio
    )
    return f"""<!doctype html>
<html lang="es">
<head>
{_landing_head(titulo, descripcion, canonical, json_ld)}
</head>
<body>
<div class="emoji">{info['emoji']}</div>
<h1>{info['nombre']} en {ciudad_nombre}</h1>
<p>{descripcion}</p>
<h2>Servicios más solicitados</h2>
<ul>{servicios_li}</ul>
<h2>¿Por qué Multiservicios Provenza?</h2>
<ul>
  <li>✅ Profesionales verificados, con reseñas reales de otros clientes</li>
  <li>💶 Presupuesto claro antes de empezar el trabajo</li>
  <li>🔒 Pago seguro dentro de la app</li>
  <li>⚡ Disponibilidad urgente en {ciudad_nombre}</li>
</ul>
<div class="cta">
  <p><strong>Multiservicios Provenza</strong> llega muy pronto a {ciudad_nombre}.</p>
  <p><a href="/landing">Ver todos los oficios y ciudades</a></p>
</div>
<h2>Otros oficios en {ciudad_nombre}</h2>
<p>{otros_gremios}</p>
<footer>Multiservicios Provenza · profesionales del hogar en Bilbao, Madrid y Barcelona</footer>
</body>
</html>"""


def _landing_index_html() -> str:
    canonical = f"{_landing_base_url()}/landing"
    titulo = "Profesionales del hogar verificados | Multiservicios Provenza"
    descripcion = "Fontaneros, electricistas, cerrajeros y más de 10 oficios del hogar, verificados y con presupuesto claro, en Bilbao, Madrid y Barcelona."
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "Multiservicios Provenza",
        "description": descripcion,
    }
    filas = ""
    for g, info in GREMIOS_LANDING.items():
        enlaces = " · ".join(f'<a href="/landing/{g}/{c}">{nombre}</a>' for c, nombre in CIUDADES_LANDING.items())
        filas += f"<li>{info['emoji']} <strong>{info['nombre']}</strong>: {enlaces}</li>"
    return f"""<!doctype html>
<html lang="es">
<head>
{_landing_head(titulo, descripcion, canonical, json_ld)}
</head>
<body>
<div class="emoji">🏠</div>
<h1>Profesionales del hogar cerca de ti</h1>
<p>{descripcion}</p>
<ul>{filas}</ul>
<footer>Multiservicios Provenza</footer>
</body>
</html>"""


@app.get("/landing")
def landing_index():
    from fastapi.responses import Response
    return Response(content=_landing_index_html(), media_type="text/html")


@app.get("/landing/{gremio}/{ciudad}")
def landing_gremio_ciudad(gremio: str, ciudad: str):
    from fastapi.responses import Response
    if gremio not in GREMIOS_LANDING or ciudad not in CIUDADES_LANDING:
        raise HTTPException(status_code=404, detail="Página no encontrada")
    return Response(content=_landing_html(gremio, ciudad), media_type="text/html")


@app.get("/sitemap.xml")
def sitemap_xml():
    from fastapi.responses import Response
    base = _landing_base_url()
    urls = [f"{base}/", f"{base}/landing"]
    for g in GREMIOS_LANDING:
        for c in CIUDADES_LANDING:
            urls.append(f"{base}/landing/{g}/{c}")
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt")
def robots_txt():
    from fastapi.responses import Response
    base = _landing_base_url()
    txt = f"User-agent: *\nAllow: /landing\nDisallow: /admin\nDisallow: /uploads\nSitemap: {base}/sitemap.xml\n"
    return Response(content=txt, media_type="text/plain")


# ─── TÉRMINOS Y PRIVACIDAD (páginas públicas para App Store / Play Store) ──────
# Apple y Google exigen una URL pública a la política de privacidad en la ficha
# de la tienda; no basta con una pantalla dentro de la app.

def _legal_page_html(titulo: str, subtitulo: str, secciones: list) -> str:
    canonical_slug = "privacidad" if "privacidad" in titulo.lower() else "terminos"
    canonical = f"{_landing_base_url()}/{canonical_slug}"
    secciones_html = "".join(
        f"<h2>{sec['titulo']}</h2><p>{sec['texto']}</p>" for sec in secciones
    )
    return f"""<!doctype html>
<html lang="es">
<head>
{_landing_head(titulo, subtitulo, canonical, {
    "@context": "https://schema.org", "@type": "WebPage", "name": titulo,
})}
</head>
<body>
<h1>{titulo}</h1>
<p style="color:#8a94a6;font-size:13px;">{subtitulo}</p>
{secciones_html}
<footer>Multiservicios Provenza · <a href="mailto:soporte@fontap.app">soporte@fontap.app</a></footer>
</body>
</html>"""


@app.get("/terminos", include_in_schema=False)
def pagina_terminos():
    from fastapi.responses import Response
    secciones = [
        {"titulo": "1. Qué es Multiservicios Provenza", "texto":
            "Multiservicios Provenza es una plataforma de intermediación que conecta a clientes que necesitan un servicio "
            "para el hogar (fontanería, electricidad, cerrajería y otros oficios) con profesionales independientes que ofrecen esos servicios. "
            "Multiservicios Provenza no presta directamente estos servicios: actúa como intermediario entre ambas partes."},
        {"titulo": "2. Registro y cuenta", "texto":
            "Debes ser mayor de 18 años para registrarte y contratar o prestar servicios a través de Multiservicios Provenza. "
            "Para usar Multiservicios Provenza debes registrarte con datos reales y mantener actualizada tu información de "
            "contacto. Eres responsable de la actividad realizada desde tu cuenta y de la confidencialidad de "
            "tu contraseña. Los profesionales declaran contar con la cualificación y, en su caso, los seguros "
            "necesarios para ejercer su actividad."},
        {"titulo": "3. Precios y pagos", "texto":
            "El precio de cada servicio lo fija libremente el profesional y se muestra al cliente antes de "
            "confirmar el pago. Multiservicios Provenza aplica una comisión sobre el importe cobrado por servicios pagados a "
            "través de la plataforma. Los pagos en efectivo o Bizum se acuerdan directamente entre cliente y profesional; "
            "Multiservicios Provenza no es responsable de disputas sobre pagos realizados fuera de la app."},
        {"titulo": "4. Cancelaciones", "texto":
            "Tanto el cliente como el profesional pueden cancelar una solicitud antes de que el servicio se "
            "marque como pagado. Cancelaciones repetidas o de mala fe pueden dar lugar a la suspensión de la cuenta."},
        {"titulo": "5. Reseñas", "texto":
            "Al finalizar un servicio pagado, cliente y profesional pueden valorarse mutuamente. Las reseñas deben "
            "reflejar experiencias reales; Multiservicios Provenza puede retirar reseñas falsas, ofensivas o que infrinjan estos términos."},
        {"titulo": "6. Responsabilidad", "texto":
            "Multiservicios Provenza no es parte del contrato de servicio entre cliente y profesional y no responde por la calidad, "
            "seguridad o resultado del trabajo realizado. Cualquier incidencia relativa a la ejecución del servicio "
            "debe resolverse entre las partes; Multiservicios Provenza puede mediar de buena fe pero no garantiza el resultado."},
        {"titulo": "7. Protección de datos", "texto":
            'Tratamos tus datos personales según nuestra <a href="/privacidad">Política de Privacidad</a>. '
            "No vendemos tus datos a terceros. Puedes solicitar la eliminación de tu cuenta y datos asociados "
            "en cualquier momento desde la app o escribiendo a soporte."},
        {"titulo": "8. Cambios en estos términos", "texto":
            "Podemos actualizar estos términos para reflejar cambios en el servicio o en la normativa aplicable. "
            "Si los cambios son sustanciales, te avisaremos dentro de la app antes de que entren en vigor."},
    ]
    html = _legal_page_html(
        "Términos y condiciones · Multiservicios Provenza",
        "Última actualización: julio de 2026",
        secciones,
    )
    return Response(content=html, media_type="text/html")


@app.get("/privacidad", include_in_schema=False)
def pagina_privacidad():
    from fastapi.responses import Response
    secciones = [
        {"titulo": "1. Quién trata tus datos", "texto":
            "Multiservicios Provenza es responsable del tratamiento de los datos personales que recoge a través de la app "
            "y este sitio web. Para cualquier consulta sobre tus datos, escribe a soporte@fontap.app."},
        {"titulo": "2. Qué datos recogemos", "texto":
            "Nombre, email y teléfono al registrarte; ubicación aproximada mientras usas el mapa o solicitas un servicio "
            "urgente; fotos que subas al chat, a tu perfil o como evidencia de un trabajo; documento de identidad (DNI/NIE) "
            "de los profesionales, solo para verificar su identidad; historial de servicios, mensajes de chat y valoraciones; "
            "y datos de pago procesados directamente por Stripe (no almacenamos números de tarjeta en nuestros servidores)."},
        {"titulo": "3. Para qué los usamos y con qué base legal", "texto":
            "Para prestar el servicio de intermediación (ejecución del contrato): mostrar tu solicitud a profesionales "
            "cercanos, gestionar pagos, permitir el chat y los recordatorios de cita. Para verificar la identidad de los "
            "profesionales y prevenir fraude (interés legítimo). Para enviarte notificaciones del servicio (ejecución del "
            "contrato) y, si lo autorizas, sincronizar tu calendario de Google (consentimiento)."},
        {"titulo": "4. Con quién compartimos datos", "texto":
            "Con la otra parte de un servicio (cliente/profesional), lo mínimo necesario para completarlo. Con proveedores "
            "que tratan datos en nuestro nombre: Stripe (pagos), Google (inicio de sesión, Google Calendar y mapas), y "
            "nuestro proveedor de hosting para la base de datos y los archivos subidos. No vendemos tus datos a terceros "
            "con fines publicitarios."},
        {"titulo": "5. Documentos de identidad de profesionales", "texto":
            "Las fotos de DNI/NIE que suben los profesionales para verificarse se usan exclusivamente para que el equipo "
            "de Multiservicios Provenza confirme su identidad, con acceso restringido al panel de administración, y no se "
            "muestran públicamente ni se comparten con clientes."},
        {"titulo": "6. Cuánto tiempo conservamos los datos", "texto":
            "Mientras tu cuenta esté activa. Si eliminas tu cuenta, borramos o anonimizamos tus datos personales salvo los "
            "que debamos conservar por obligación legal (por ejemplo, facturación)."},
        {"titulo": "7. Tus derechos", "texto":
            "Puedes acceder a tus datos, corregirlos, solicitar su eliminación o pedir una copia de los mismos escribiendo "
            "a soporte@fontap.app o, cuando esté disponible en la app, desde los ajustes de tu cuenta. También puedes "
            "eliminar tu cuenta tú mismo en cualquier momento desde la app."},
        {"titulo": "8. Menores de edad", "texto":
            "Multiservicios Provenza no está dirigida a menores de 18 años y no permite el registro de menores."},
        {"titulo": "9. Cambios en esta política", "texto":
            "Podemos actualizar esta política para reflejar cambios en el servicio o en la normativa aplicable. Si los "
            "cambios son sustanciales, te avisaremos dentro de la app antes de que entren en vigor."},
    ]
    html = _legal_page_html(
        "Política de privacidad · Multiservicios Provenza",
        "Última actualización: julio de 2026",
        secciones,
    )
    return Response(content=html, media_type="text/html")


@app.get("/eliminar-cuenta", include_in_schema=False)
def pagina_eliminar_cuenta():
    from fastapi.responses import Response
    secciones = [
        {"titulo": "Cómo eliminar tu cuenta de Multiservicios Provenza", "texto":
            "Puedes eliminar tu cuenta de Multiservicios Provenza (cliente o profesional) tú mismo, en cualquier "
            "momento, directamente desde la app, siguiendo estos pasos: "
            "1. Abre la app Multiservicios Provenza e inicia sesión. "
            "2. Entra en tu perfil o Ajustes de cuenta. "
            "3. Pulsa el botón \"Eliminar cuenta\". "
            "4. Confirma la eliminación cuando se te pida. "
            "La cuenta se elimina de inmediato al confirmar, sin esperas ni pasos adicionales."},
        {"titulo": "Qué datos se eliminan", "texto":
            "Al eliminar tu cuenta borramos o anonimizamos de inmediato: tu nombre, email, teléfono, foto de perfil, "
            "descripción y ubicación. Tu sesión queda invalidada al instante en todos los dispositivos. Dejas de "
            "aparecer como profesional disponible y ya no puedes iniciar sesión con esa cuenta."},
        {"titulo": "Qué datos se conservan y por qué", "texto":
            "Por obligación legal (normativa fiscal y de facturación en España), conservamos de forma anonimizada el "
            "historial de servicios ya realizados y sus importes durante el plazo exigido por ley, sin datos que te "
            "identifiquen personalmente. Los mensajes de chat y valoraciones asociados a servicios ya completados se "
            "conservan igualmente anonimizados, ya que también forman parte del historial del servicio. Ningún dato "
            "de contacto (email, teléfono) ni foto se conserva tras la eliminación."},
        {"titulo": "Solicitar la eliminación sin acceso a la app", "texto":
            "Si no puedes acceder a la app, puedes solicitar la eliminación de tu cuenta y datos escribiendo a "
            "soporte@fontap.app desde el email registrado en tu cuenta, indicando tu nombre y email de la cuenta a eliminar."},
    ]
    html = _legal_page_html(
        "Eliminar cuenta · Multiservicios Provenza",
        "Cómo eliminar tu cuenta y qué ocurre con tus datos",
        secciones,
    )
    return Response(content=html, media_type="text/html")


def _estado_dependencias(db: Session):
    dependencias = []

    try:
        db.execute(text("SELECT 1"))
        dependencias.append(("Base de datos", True, "Conectada"))
    except Exception:
        dependencias.append(("Base de datos", False, "No responde"))

    correo_configurado = bool((BREVO_API_KEY and BREVO_FROM_EMAIL) or SMTP_HOST)
    dependencias.append((
        "Correo (verificación, recuperación de contraseña)",
        correo_configurado,
        "Configurado (Brevo)" if (BREVO_API_KEY and BREVO_FROM_EMAIL) else ("Configurado (SMTP)" if SMTP_HOST else "Sin configurar (los códigos se muestran solo en los logs)"),
    ))

    dependencias.append((
        "Pagos (Stripe)",
        bool(STRIPE_SECRET_KEY),
        "Configurado" if STRIPE_SECRET_KEY else "Sin configurar (pago con Stripe no disponible)",
    ))

    google_ok = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    dependencias.append((
        "Google Calendar",
        google_ok,
        "Configurado" if google_ok else "Sin configurar (falta GOOGLE_CLIENT_ID y/o GOOGLE_CLIENT_SECRET)",
    ))

    dependencias.append((
        "Notificaciones push",
        True,
        "Expo Push Service (sin configuración adicional)",
    ))

    return dependencias

@app.get("/status", include_in_schema=False)
def pagina_estado(db: Session = Depends(get_db)):
    from fastapi.responses import HTMLResponse
    dependencias = _estado_dependencias(db)
    todo_ok = all(ok for _, ok, _ in dependencias)
    filas = "".join(
        f'<tr><td>{nombre}</td><td><span class="badge {"ok" if ok else "mal"}">{"● Operativo" if ok else "● Atención"}</span></td><td>{detalle}</td></tr>'
        for nombre, ok, detalle in dependencias
    )
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estado del servicio · Multiservicios Provenza</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background: #0f1115; color: #e6e8eb; max-width: 720px; margin: 0 auto; padding: 40px 20px; }}
  h1 {{ font-size: 22px; }}
  .resumen {{ display: inline-block; padding: 8px 16px; border-radius: 20px; font-weight: 600; margin-bottom: 24px; }}
  .resumen.ok {{ background: #16321f; color: #4ade80; }}
  .resumen.mal {{ background: #3a1f1f; color: #f87171; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 12px 8px; border-bottom: 1px solid #262a31; font-size: 14px; }}
  .badge.ok {{ color: #4ade80; }}
  .badge.mal {{ color: #f87171; }}
  .footer {{ margin-top: 24px; font-size: 12px; color: #8a8f98; }}
</style>
</head>
<body>
  <h1>Estado del servicio</h1>
  <div class="resumen {'ok' if todo_ok else 'mal'}">{'✓ Todo operativo' if todo_ok else '⚠ Algún componente necesita atención'}</div>
  <table>{filas}</table>
  <div class="footer">Actualizado en cada carga de esta página.</div>
</body>
</html>"""
    return HTMLResponse(html)

@app.post("/migrar-db")
def migrar_db(current_user: dict = Depends(auth.get_current_user)):
    _verificar_admin(current_user)
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
    if len(usuario.password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
    existe = db.query(models.Usuario).filter(models.Usuario.email == email).first()
    if existe:
        raise HTTPException(status_code=400, detail="Email ya registrado")
    if usuario.tipo == "fontanero" and usuario.telefono:
        telefono_en_uso = db.query(models.Fontanero).filter(
            models.Fontanero.telefono == usuario.telefono,
            models.Fontanero.telefono != "",
        ).first()
        if telefono_en_uso:
            raise HTTPException(status_code=400, detail="Este teléfono ya está registrado en otra cuenta de profesional")
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
        nuevo_fontanero = get_or_create_fontanero(db, nuevo.id, usuario.gremio)
        nuevo_fontanero.codigo_referido = secrets.token_hex(3).upper()
        if usuario.codigo_referido:
            invitador = db.query(models.Fontanero).filter(
                models.Fontanero.codigo_referido == usuario.codigo_referido.strip().upper(),
                models.Fontanero.gremio == usuario.gremio,
            ).first()
            if invitador:
                # El beneficio para el invitador (comisión reducida 90 días) se concede
                # recién cuando el referido verifica su email (ver /auth/verificar-email),
                # no aquí: si no, cualquiera podría inflarlo creando cuentas sin verificar.
                nuevo_fontanero.referido_por_id = invitador.id
        db.commit()
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
# se bloquea temporalmente ese email para el `scope` en cuestión (login, o probar
# códigos de reseteo/verificación). En memoria (se reinicia con el proceso).
MAX_INTENTOS_LOGIN = 5
BLOQUEO_LOGIN_MINUTOS = 15
_intentos_por_scope = {}  # (scope, email) -> {"fallos": int, "bloqueado_hasta": datetime | None}

def _intento_bloqueado(scope: str, email: str, max_intentos: int = MAX_INTENTOS_LOGIN, minutos_bloqueo: int = BLOQUEO_LOGIN_MINUTOS):
    clave = (scope, email)
    registro = _intentos_por_scope.get(clave)
    if not registro or not registro.get("bloqueado_hasta"):
        return None
    restante = registro["bloqueado_hasta"] - datetime.datetime.utcnow()
    if restante.total_seconds() <= 0:
        _intentos_por_scope.pop(clave, None)
        return None
    return int(restante.total_seconds() // 60) + 1

def _registrar_fallo(scope: str, email: str, max_intentos: int = MAX_INTENTOS_LOGIN, minutos_bloqueo: int = BLOQUEO_LOGIN_MINUTOS):
    clave = (scope, email)
    registro = _intentos_por_scope.setdefault(clave, {"fallos": 0, "bloqueado_hasta": None})
    registro["fallos"] += 1
    if registro["fallos"] >= max_intentos:
        registro["bloqueado_hasta"] = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutos_bloqueo)

def _limpiar_intentos(scope: str, email: str):
    _intentos_por_scope.pop((scope, email), None)

def _login_bloqueado(email: str):
    return _intento_bloqueado("login", email)

def _registrar_fallo_login(email: str):
    _registrar_fallo("login", email)

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
    _limpiar_intentos("login", email_normalizado)
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
    if BREVO_API_KEY and BREVO_FROM_EMAIL:
        try:
            r = _http.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
                json={
                    "sender": {"name": BREVO_FROM_NOMBRE, "email": BREVO_FROM_EMAIL},
                    "to": [{"email": destinatario}],
                    "subject": asunto,
                    "textContent": cuerpo,
                },
                timeout=10,
            )
            if r.status_code >= 400:
                print(f"[{etiqueta}] Error de Brevo enviando a {destinatario}: {r.status_code} {r.text}")
        except Exception as e:
            print(f"[{etiqueta}] Error enviando email (Brevo) a {destinatario}: {e}")
        return
    if not SMTP_HOST:
        print(f"[{etiqueta}] Correo no configurado (ni Brevo ni SMTP). Código para {destinatario}: {codigo or '(ver cuerpo)'}")
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
        print(f"[{etiqueta}] Error enviando email (SMTP) a {destinatario}: {e}")

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

MAX_ENVIOS_CODIGO = 3
BLOQUEO_ENVIO_CODIGO_MINUTOS = 15

@app.post("/auth/olvide-password")
def olvide_password(datos: OlvidePasswordDatos, db: Session = Depends(get_db)):
    email_normalizado = _norm_email(datos.email)
    minutos = _intento_bloqueado("reset-envio", email_normalizado, MAX_ENVIOS_CODIGO, BLOQUEO_ENVIO_CODIGO_MINUTOS)
    if minutos:
        raise HTTPException(status_code=429, detail=f"Demasiadas solicitudes. Inténtalo de nuevo en {minutos} min")
    usuario = db.query(models.Usuario).filter(models.Usuario.email == email_normalizado).first()
    if usuario:
        _registrar_fallo("reset-envio", email_normalizado, MAX_ENVIOS_CODIGO, BLOQUEO_ENVIO_CODIGO_MINUTOS)
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

MAX_INTENTOS_CODIGO = 8
BLOQUEO_CODIGO_MINUTOS = 15

@app.post("/auth/resetear-password")
def resetear_password(datos: ResetPasswordDatos, db: Session = Depends(get_db)):
    email_normalizado = _norm_email(datos.email)
    minutos = _intento_bloqueado("reset", email_normalizado, MAX_INTENTOS_CODIGO, BLOQUEO_CODIGO_MINUTOS)
    if minutos:
        raise HTTPException(status_code=429, detail=f"Demasiados intentos. Inténtalo de nuevo en {minutos} min")
    usuario = db.query(models.Usuario).filter(models.Usuario.email == email_normalizado).first()
    error_generico = HTTPException(status_code=400, detail="Código inválido o caducado")
    if not usuario:
        _registrar_fallo("reset", email_normalizado, MAX_INTENTOS_CODIGO, BLOQUEO_CODIGO_MINUTOS)
        raise error_generico
    reset = db.query(models.PasswordReset).filter(
        models.PasswordReset.usuario_id == usuario.id,
        models.PasswordReset.token == datos.token.strip().upper(),
        models.PasswordReset.usado == False,
    ).order_by(models.PasswordReset.id.desc()).first()
    if not reset or reset.expira < datetime.datetime.utcnow():
        _registrar_fallo("reset", email_normalizado, MAX_INTENTOS_CODIGO, BLOQUEO_CODIGO_MINUTOS)
        raise error_generico
    if len(datos.nueva_password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
    usuario.password_hash = auth.hashear_password(datos.nueva_password)
    reset.usado = True
    _limpiar_intentos("reset", email_normalizado)
    db.commit()
    return {"mensaje": "Contraseña actualizada"}

class VerificarEmailDatos(BaseModel):
    email: str
    token: str

@app.post("/auth/verificar-email")
def verificar_email(datos: VerificarEmailDatos, db: Session = Depends(get_db)):
    email_normalizado = _norm_email(datos.email)
    minutos = _intento_bloqueado("verify", email_normalizado, MAX_INTENTOS_CODIGO, BLOQUEO_CODIGO_MINUTOS)
    if minutos:
        raise HTTPException(status_code=429, detail=f"Demasiados intentos. Inténtalo de nuevo en {minutos} min")
    usuario = db.query(models.Usuario).filter(models.Usuario.email == email_normalizado).first()
    error_generico = HTTPException(status_code=400, detail="Código inválido o caducado")
    if not usuario:
        _registrar_fallo("verify", email_normalizado, MAX_INTENTOS_CODIGO, BLOQUEO_CODIGO_MINUTOS)
        raise error_generico
    if usuario.email_verificado:
        return {"mensaje": "Email ya verificado"}
    verificacion = db.query(models.VerificacionEmail).filter(
        models.VerificacionEmail.usuario_id == usuario.id,
        models.VerificacionEmail.token == datos.token.strip().upper(),
        models.VerificacionEmail.usado == False,
    ).order_by(models.VerificacionEmail.id.desc()).first()
    if not verificacion or verificacion.expira < datetime.datetime.utcnow():
        _registrar_fallo("verify", email_normalizado, MAX_INTENTOS_CODIGO, BLOQUEO_CODIGO_MINUTOS)
        raise error_generico
    usuario.email_verificado = True
    verificacion.usado = True
    if usuario.tipo == "fontanero":
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario.id).first()
        if fontanero and fontanero.referido_por_id:
            invitador = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero.referido_por_id).first()
            if invitador:
                invitador.referido_hasta = datetime.datetime.utcnow() + datetime.timedelta(days=90)
                if invitador.usuario_id:
                    _crear_notificacion(
                        db, invitador.usuario_id, "¡Gracias por invitar a un colega!",
                        f"{usuario.nombre} se ha unido con tu código. Tendrás comisión reducida durante 90 días.",
                        "referido", fontanero.id,
                    )
    _limpiar_intentos("verify", email_normalizado)
    db.commit()
    return {"mensaje": "Email verificado"}

class ReenviarVerificacionDatos(BaseModel):
    email: str

@app.post("/auth/reenviar-verificacion")
def reenviar_verificacion(datos: ReenviarVerificacionDatos, db: Session = Depends(get_db)):
    email_normalizado = _norm_email(datos.email)
    minutos = _intento_bloqueado("verify-envio", email_normalizado, MAX_ENVIOS_CODIGO, BLOQUEO_ENVIO_CODIGO_MINUTOS)
    if minutos:
        raise HTTPException(status_code=429, detail=f"Demasiadas solicitudes. Inténtalo de nuevo en {minutos} min")
    usuario = db.query(models.Usuario).filter(models.Usuario.email == email_normalizado).first()
    if usuario and not usuario.email_verificado:
        _registrar_fallo("verify-envio", email_normalizado, MAX_ENVIOS_CODIGO, BLOQUEO_ENVIO_CODIGO_MINUTOS)
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
    nonce: Optional[str] = None

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
    if not GOOGLE_CLIENT_ID:
        # Sin GOOGLE_CLIENT_ID configurado no hay forma de comprobar que el token es
        # para esta app: mejor rechazar que aceptar cualquier id_token válido emitido
        # para OTRA aplicación de Google (confusión de audiencia).
        raise HTTPException(status_code=501, detail="Login con Google no configurado en el servidor")
    if info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Token de Google no corresponde a esta app")
    # El frontend genera un nonce por intento de login y lo manda de vuelta aquí:
    # comprobarlo contra el que Google firmó dentro del id_token evita que un
    # id_token capturado (red, logs, dispositivo comprometido) se reutilice más
    # tarde en un login distinto. Opcional para no romper builds de la app ya
    # publicadas que todavía no envían este campo.
    if datos.nonce and info.get("nonce") != datos.nonce:
        raise HTTPException(status_code=401, detail="Token de Google inválido (nonce no coincide)")

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

@app.get("/usuarios/{usuario_id}/aniversario")
def ver_aniversario(
    usuario_id: int,
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Insignia de aniversario de cuenta: años en la plataforma + resumen del último año."""
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes ver otra cuenta")
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    ahora = datetime.datetime.utcnow()
    miembro_desde = usuario.creado_en
    años = ahora.year - miembro_desde.year - (
        (ahora.month, ahora.day) < (miembro_desde.month, miembro_desde.day)
    )
    proximo_aniversario = miembro_desde.replace(year=ahora.year)
    if proximo_aniversario < ahora:
        proximo_aniversario = proximo_aniversario.replace(year=ahora.year + 1)
    dias_para_aniversario = (proximo_aniversario.date() - ahora.date()).days
    es_aniversario = dias_para_aniversario <= 7 or (ahora - miembro_desde).days % 365 <= 7

    hace_un_año = ahora - datetime.timedelta(days=365)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario_id).first()
    if fontanero:
        servicios = db.query(models.Servicio).filter(
            models.Servicio.fontanero_id == fontanero.id,
            models.Servicio.estado == "pagado",
            models.Servicio.creado_en >= hace_un_año,
        ).all()
        total = sum(s.precio or 0 for s in servicios)
        resumen = {"servicios_completados": len(servicios), "total_ganado": round(total, 2)}
    else:
        servicios = db.query(models.Servicio).filter(
            models.Servicio.cliente_id == usuario_id,
            models.Servicio.estado == "pagado",
            models.Servicio.creado_en >= hace_un_año,
        ).all()
        total = sum(s.precio or 0 for s in servicios)
        resumen = {"servicios_contratados": len(servicios), "total_gastado": round(total, 2)}

    return {
        "miembro_desde": miembro_desde,
        "años": max(años, 0),
        "es_aniversario": es_aniversario,
        "resumen_ultimo_año": resumen,
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

def _anonimizar_usuario(db: Session, usuario_id: int, usuario) -> None:
    """Borra los datos personales y anonimiza el historial (los servicios y reseñas
    se conservan sin datos identificativos, RGPD). No hace commit: lo hace el caller."""
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
    # Revoca cualquier JWT ya emitido para esta cuenta (get_current_user comprueba
    # este flag en cada request): sin esto, una sesión ya iniciada seguiría
    # funcionando hasta 7 días después de "eliminada" la cuenta.
    usuario.bloqueado = True


@app.delete("/usuarios/{usuario_id}")
def eliminar_cuenta(
    usuario_id: int,
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Elimina la cuenta propia."""
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes eliminar otra cuenta")
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    _anonimizar_usuario(db, usuario_id, usuario)
    db.commit()
    return {"mensaje": "Cuenta eliminada"}

# ─── FONTANEROS ────────────────────────────────────────────────────────────────

@app.get("/fontaneros", response_model=List[schemas.FontaneroRespuesta])
def listar_fontaneros(
    gremio: Optional[str] = None,
    ciudad: Optional[str] = None,
    cliente_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    # Excluye profesionales cuyo usuario esté vetado por el admin, o que aún no
    # hayan verificado su email (para que no aparezcan como contratables con un
    # email inventado que nunca llegarán a confirmar).
    query = db.query(models.Fontanero).outerjoin(
        models.Usuario, models.Fontanero.usuario_id == models.Usuario.id
    ).filter(
        models.Fontanero.disponible == True,
        or_(models.Usuario.bloqueado == False, models.Usuario.bloqueado == None, models.Fontanero.usuario_id == None),
        or_(models.Usuario.email_verificado == True, models.Fontanero.usuario_id == None),
    )
    if gremio:
        query = query.filter(models.Fontanero.gremio == gremio)
    if ciudad:
        query = query.filter(func.lower(models.Fontanero.zona) == ciudad.lower())
    if cliente_id and cliente_id == current_user["id"]:
        bloqueados = db.query(models.ListaNegraCliente.fontanero_id).filter(
            models.ListaNegraCliente.cliente_id == cliente_id
        )
        query = query.filter(~models.Fontanero.id.in_(bloqueados))
    fontaneros = query.all()

    precios_min = dict(
        db.query(models.ServicioFontanero.fontanero_id, func.min(models.ServicioFontanero.precio))
        .filter(models.ServicioFontanero.activo == True)
        .group_by(models.ServicioFontanero.fontanero_id)
        .all()
    )
    servicios_por_fontanero = {}
    for fontanero_id, nombre in (
        db.query(models.ServicioFontanero.fontanero_id, models.ServicioFontanero.nombre)
        .filter(models.ServicioFontanero.activo == True)
        .all()
    ):
        servicios_por_fontanero.setdefault(fontanero_id, []).append(nombre)

    resultado = []
    for f in fontaneros:
        item = schemas.FontaneroRespuesta.model_validate(f)
        item.precio_desde = precios_min.get(f.id)
        item.servicios = servicios_por_fontanero.get(f.id, [])
        item.codigo_referido = None  # solo le sirve al propio dueño (ver /fontaneros/{id}/perfil)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    pasa_a_disponible = datos.disponible and not fontanero.disponible
    fontanero.disponible = datos.disponible
    if datos.disponible_24h is not None:
        fontanero.disponible_24h = datos.disponible_24h
    db.commit()

    if pasa_a_disponible:
        espera = db.query(models.ListaEspera).filter(models.ListaEspera.gremio == fontanero.gremio).all()
        for e in espera:
            _crear_notificacion(db, e.cliente_id, "¡Hay alguien libre!",
                                 f"Un profesional de {fontanero.gremio} ya está disponible", "lista_espera", fontanero.id)
            db.delete(e)
        if espera:
            db.commit()

    return {"mensaje": "Disponibilidad actualizada"}

# ─── LISTA DE ESPERA ────────────────────────────────────────────────────────────

@app.post("/lista-espera", response_model=schemas.ListaEsperaRespuesta)
def apuntarse_lista_espera(
    datos: schemas.ListaEsperaCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if datos.gremio not in GREMIOS_VALIDOS:
        raise HTTPException(status_code=422, detail="Gremio no válido")
    existente = db.query(models.ListaEspera).filter(
        models.ListaEspera.cliente_id == current_user["id"],
        models.ListaEspera.gremio == datos.gremio,
    ).first()
    if existente:
        return existente
    entrada = models.ListaEspera(cliente_id=current_user["id"], gremio=datos.gremio)
    db.add(entrada)
    db.commit()
    db.refresh(entrada)
    return entrada

@app.get("/lista-espera", response_model=List[schemas.ListaEsperaRespuesta])
def ver_lista_espera(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.ListaEspera).filter(
        models.ListaEspera.cliente_id == current_user["id"]
    ).all()

@app.delete("/lista-espera/{gremio}")
def salir_lista_espera(
    gremio: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.ListaEspera).filter(
        models.ListaEspera.cliente_id == current_user["id"],
        models.ListaEspera.gremio == gremio,
    ).delete()
    db.commit()
    return {"mensaje": "Has salido de la lista de espera"}

@app.put("/fontaneros/{fontanero_id}/ubicacion")
def actualizar_ubicacion(
    fontanero_id: int,
    datos: schemas.UbicacionActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = get_or_create_fontanero(db, fontanero_id)
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.latitud = datos.latitud
    fontanero.longitud = datos.longitud
    fontanero.ubicacion_actualizada = models.utcnow()
    _avisar_si_esta_cerca(db, fontanero)
    db.commit()
    return {"mensaje": "Ubicación actualizada"}

@app.get("/fontaneros/{fontanero_id}/solicitudes")
def ver_solicitudes_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = get_or_create_fontanero(db, fontanero_id)
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    bloqueado_por = db.query(models.ListaNegraCliente.cliente_id).filter(
        models.ListaNegraCliente.fontanero_id == fontanero.id
    )
    pendientes = db.query(models.Servicio).filter(
        models.Servicio.estado == "pendiente",
        models.Servicio.es_consulta == False,
        or_(
            models.Servicio.fontanero_id == None,
            models.Servicio.fontanero_id == fontanero.id,
        ),
        ~models.Servicio.cliente_id.in_(bloqueado_por),
    ).all()

    propias = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado.in_(["aceptado", "precio_enviado", "precio_aceptado", "en_camino", "pago_pendiente", "pagado", "completado"]),
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
            "metodo_pago": s.metodo_pago,
            "fecha": str(s.fecha) if s.fecha else None,
            "cliente_nombre": cliente.nombre if cliente else "Cliente",
            "zona": "Bilbao",
            "eta_minutos": s.eta_minutos,
            "latitud_cliente": s.latitud_cliente,
            "longitud_cliente": s.longitud_cliente,
        })
    return resultado

# ─── SERVICIOS ─────────────────────────────────────────────────────────────────

def _notificar_urgencia(db: Session, nuevo, tipo: str, solo_ciudad: bool):
    bloqueados = db.query(models.ListaNegraCliente.fontanero_id).filter(
        models.ListaNegraCliente.cliente_id == nuevo.cliente_id
    )
    query = db.query(models.Fontanero).join(
        models.Usuario, models.Fontanero.usuario_id == models.Usuario.id
    ).filter(
        models.Fontanero.disponible == True,
        models.Fontanero.usuario_id != None,
        models.Fontanero.gremio == nuevo.gremio,
        models.Usuario.email_verificado == True,
        ~models.Fontanero.id.in_(bloqueados),
    )
    if solo_ciudad and nuevo.ciudad:
        query = query.filter(func.lower(models.Fontanero.zona) == nuevo.ciudad.lower())
    for f in query.all():
        _crear_notificacion(db, f.usuario_id, "Nueva solicitud urgente", f"Solicitud urgente de {tipo} cerca de tu zona", "solicitud_urgente", nuevo.id)

def _crear_servicio_interno(db: Session, cliente_id: int, tipo: str, descripcion: Optional[str],
                             urgente: bool, fecha, fontanero_id: Optional[int], gremio: Optional[str],
                             ciudad: Optional[str] = None, latitud_cliente: Optional[float] = None,
                             longitud_cliente: Optional[float] = None, es_consulta: bool = False):
    fontanero_directo = None
    if fontanero_id:
        fontanero_directo = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    nuevo = models.Servicio(
        cliente_id=cliente_id,
        fontanero_id=fontanero_id,
        tipo=tipo,
        descripcion=descripcion,
        urgente=urgente,
        fecha=fecha,
        estado="pendiente",
        precio=None,
        es_consulta=es_consulta,
        gremio=fontanero_directo.gremio if fontanero_directo else gremio,
        ciudad=ciudad,
        latitud_cliente=latitud_cliente,
        longitud_cliente=longitud_cliente,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    if fontanero_directo and not es_consulta:
        _crear_notificacion(db, fontanero_directo.usuario_id, "Nueva solicitud", f"Tienes una nueva solicitud de {tipo}", "solicitud_directa", nuevo.id)
        db.commit()
    elif urgente:
        # Primero se avisa solo a los profesionales de la misma ciudad; si nadie responde
        # a tiempo, _bucle_ampliar_radio_urgencias avisará también al resto (ver más abajo).
        _notificar_urgencia(db, nuevo, tipo, solo_ciudad=bool(nuevo.ciudad))
        db.commit()
    elif not es_consulta and nuevo.gremio:
        _avisar_preferente(db, nuevo, tipo)
    return nuevo

def _avisar_preferente(db: Session, nuevo, tipo: str) -> None:
    """Si el cliente tiene marcado un "profesional de confianza" para el gremio de esta
    solicitud, le avisa primero y le da unos minutos de ventaja antes de que la vea el
    resto del mercado (ver filtro en /servicios/abiertos)."""
    favorito_preferente = db.query(models.Favorito).join(
        models.Fontanero, models.Favorito.fontanero_id == models.Fontanero.id
    ).filter(
        models.Favorito.cliente_id == nuevo.cliente_id,
        models.Favorito.preferente == True,
        models.Fontanero.gremio == nuevo.gremio,
        models.Fontanero.disponible == True,
    ).first()
    if not favorito_preferente:
        return
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == favorito_preferente.fontanero_id).first()
    if not fontanero or not fontanero.usuario_id:
        return
    nuevo.fontanero_preferente_id = fontanero.id
    nuevo.prioridad_hasta = datetime.datetime.utcnow() + datetime.timedelta(minutes=15)
    db.commit()
    _crear_notificacion(db, fontanero.usuario_id, "⭐ Un cliente te tiene como profesional de confianza",
                         f"Tienes prioridad para una nueva solicitud de {tipo} antes de que se abra al resto",
                         "solicitud_preferente", nuevo.id)
    db.commit()

@app.post("/servicios", response_model=schemas.ServicioRespuesta)
def crear_servicio(
    servicio: schemas.ServicioCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    nuevo = _crear_servicio_interno(
        db, current_user["id"], servicio.tipo, servicio.descripcion,
        servicio.urgente, servicio.fecha, servicio.fontanero_id, servicio.gremio,
        servicio.ciudad, servicio.latitud_cliente, servicio.longitud_cliente,
        servicio.es_consulta,
    )
    return schemas.ServicioRespuesta.from_orm_with_color(nuevo)

@app.get("/servicios/abiertos", response_model=List[schemas.ServicioRespuesta])
def listar_servicios_abiertos(
    gremio: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    query = db.query(models.Servicio).filter(
        models.Servicio.estado == "pendiente",
        models.Servicio.fontanero_id == None,
    )
    if gremio:
        query = query.filter(models.Servicio.gremio == gremio)
    fontanero_actual = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if fontanero_actual:
        bloqueado_por = db.query(models.ListaNegraCliente.cliente_id).filter(
            models.ListaNegraCliente.fontanero_id == fontanero_actual.id
        )
        query = query.filter(~models.Servicio.cliente_id.in_(bloqueado_por))
    servicios = query.order_by(models.Servicio.id.desc()).all()
    ahora = datetime.datetime.utcnow()
    servicios = [
        s for s in servicios
        if not (s.prioridad_hasta and s.prioridad_hasta > ahora
                and (not fontanero_actual or s.fontanero_preferente_id != fontanero_actual.id))
    ]
    resultado = []
    for s in servicios:
        data = schemas.ServicioRespuesta.from_orm_with_color(s)
        data.num_ofertas = db.query(models.Oferta).filter(
            models.Oferta.servicio_id == s.id,
            models.Oferta.estado == "pendiente",
        ).count()
        resultado.append(data)
    return resultado

# ─── SERVICIOS RECURRENTES ──────────────────────────────────────────────────────

FRECUENCIA_A_DIAS = {"semanal": 7, "quincenal": 14, "mensual": 30}

@app.post("/servicios-recurrentes", response_model=schemas.ServicioRecurrenteRespuesta)
def crear_servicio_recurrente(
    datos: schemas.ServicioRecurrenteCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if datos.gremio not in GREMIOS_VALIDOS:
        raise HTTPException(status_code=422, detail="Gremio no válido")
    plantilla = models.ServicioRecurrente(
        cliente_id=current_user["id"],
        fontanero_id=datos.fontanero_id,
        gremio=datos.gremio,
        tipo=datos.tipo,
        descripcion=datos.descripcion,
        frecuencia=datos.frecuencia,
        proxima_ejecucion=datos.proxima_ejecucion,
    )
    db.add(plantilla)
    db.commit()
    db.refresh(plantilla)
    return plantilla

@app.get("/servicios-recurrentes", response_model=List[schemas.ServicioRecurrenteRespuesta])
def listar_servicios_recurrentes(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.ServicioRecurrente).filter(
        models.ServicioRecurrente.cliente_id == current_user["id"]
    ).order_by(models.ServicioRecurrente.id.desc()).all()

@app.put("/servicios-recurrentes/{recurrente_id}", response_model=schemas.ServicioRecurrenteRespuesta)
def actualizar_servicio_recurrente(
    recurrente_id: int,
    datos: schemas.ServicioRecurrenteActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    plantilla = db.query(models.ServicioRecurrente).filter(
        models.ServicioRecurrente.id == recurrente_id,
        models.ServicioRecurrente.cliente_id == current_user["id"],
    ).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Servicio recurrente no encontrado")
    if datos.activo is not None:
        plantilla.activo = datos.activo
    if datos.frecuencia is not None:
        plantilla.frecuencia = datos.frecuencia
    db.commit()
    db.refresh(plantilla)
    return plantilla

@app.delete("/servicios-recurrentes/{recurrente_id}")
def eliminar_servicio_recurrente(
    recurrente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.ServicioRecurrente).filter(
        models.ServicioRecurrente.id == recurrente_id,
        models.ServicioRecurrente.cliente_id == current_user["id"],
    ).delete()
    db.commit()
    return {"mensaje": "Servicio recurrente cancelado"}

# ─── PROYECTOS (TABLÓN B2B) ─────────────────────────────────────────────────────
# Trabajos grandes (reformas, comunidades de vecinos...) que abarcan varios
# gremios a la vez. Los publica un administrador de fincas y los profesionales
# de los gremios implicados pueden apuntarse.

def _proyecto_con_interesados(db: Session, proyecto: models.Proyecto) -> schemas.ProyectoRespuesta:
    data = schemas.ProyectoRespuesta.model_validate(proyecto)
    data.num_interesados = db.query(models.ProyectoInteres).filter(
        models.ProyectoInteres.proyecto_id == proyecto.id
    ).count()
    return data

@app.post("/proyectos", response_model=schemas.ProyectoRespuesta)
def crear_proyecto(
    datos: schemas.ProyectoCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["tipo"] != "administrador_fincas":
        raise HTTPException(status_code=403, detail="Solo un administrador de fincas puede publicar proyectos")
    gremios_validos = [g for g in datos.gremios if g in GREMIOS_VALIDOS]
    if not gremios_validos:
        raise HTTPException(status_code=422, detail="Indica al menos un gremio válido")
    proyecto = models.Proyecto(
        administrador_id=current_user["id"],
        titulo=datos.titulo,
        descripcion=datos.descripcion,
        gremios=",".join(gremios_validos),
        ciudad=datos.ciudad,
    )
    db.add(proyecto)
    db.commit()
    db.refresh(proyecto)
    return _proyecto_con_interesados(db, proyecto)

@app.get("/proyectos", response_model=List[schemas.ProyectoRespuesta])
def listar_proyectos(
    gremio: Optional[str] = None,
    ciudad: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    query = db.query(models.Proyecto).filter(models.Proyecto.estado == "abierto")
    if gremio:
        query = query.filter(models.Proyecto.gremios.like(f"%{gremio}%"))
    if ciudad:
        query = query.filter(func.lower(models.Proyecto.ciudad) == ciudad.lower())
    proyectos = query.order_by(models.Proyecto.id.desc()).all()
    return [_proyecto_con_interesados(db, p) for p in proyectos]

@app.get("/proyectos/mios", response_model=List[schemas.ProyectoRespuesta])
def listar_mis_proyectos(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    proyectos = db.query(models.Proyecto).filter(
        models.Proyecto.administrador_id == current_user["id"]
    ).order_by(models.Proyecto.id.desc()).all()
    return [_proyecto_con_interesados(db, p) for p in proyectos]

@app.put("/proyectos/{proyecto_id}", response_model=schemas.ProyectoRespuesta)
def actualizar_proyecto(
    proyecto_id: int,
    datos: schemas.ProyectoActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    proyecto = db.query(models.Proyecto).filter(
        models.Proyecto.id == proyecto_id,
        models.Proyecto.administrador_id == current_user["id"],
    ).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if datos.estado is not None:
        proyecto.estado = datos.estado
    db.commit()
    db.refresh(proyecto)
    return _proyecto_con_interesados(db, proyecto)

@app.post("/proyectos/{proyecto_id}/interes", response_model=schemas.ProyectoInteresRespuesta)
def expresar_interes_proyecto(
    proyecto_id: int,
    datos: schemas.ProyectoInteresCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    proyecto = db.query(models.Proyecto).filter(models.Proyecto.id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    existente = db.query(models.ProyectoInteres).filter(
        models.ProyectoInteres.proyecto_id == proyecto_id,
        models.ProyectoInteres.fontanero_id == fontanero.id,
    ).first()
    if existente:
        return existente
    interes = models.ProyectoInteres(proyecto_id=proyecto_id, fontanero_id=fontanero.id, mensaje=datos.mensaje)
    db.add(interes)
    _crear_notificacion(db, proyecto.administrador_id, "Nuevo interesado en tu proyecto",
                         f"{fontanero.nombre} está interesado en \"{proyecto.titulo}\"", "proyecto_interes", proyecto_id)
    db.commit()
    db.refresh(interes)
    return interes

@app.get("/proyectos/{proyecto_id}/interesados", response_model=List[schemas.ProyectoInteresRespuesta])
def listar_interesados_proyecto(
    proyecto_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    proyecto = db.query(models.Proyecto).filter(
        models.Proyecto.id == proyecto_id,
        models.Proyecto.administrador_id == current_user["id"],
    ).first()
    if not proyecto:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    interesados = db.query(models.ProyectoInteres).filter(
        models.ProyectoInteres.proyecto_id == proyecto_id
    ).all()
    resultado = []
    for i in interesados:
        data = schemas.ProyectoInteresRespuesta.model_validate(i)
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == i.fontanero_id).first()
        if fontanero:
            data.fontanero_nombre = fontanero.nombre
            data.fontanero_valoracion = fontanero.valoracion
        resultado.append(data)
    return resultado

# ─── BOLSA DE AYUDANTES/APRENDICES ─────────────────────────────────────────────
# Un profesional que necesita ayuda puntual o un aprendiz publica una oferta;
# otros profesionales del mismo gremio pueden postularse.

def _oferta_empleo_con_datos(db: Session, oferta: models.OfertaEmpleo) -> schemas.OfertaEmpleoRespuesta:
    data = schemas.OfertaEmpleoRespuesta.model_validate(oferta)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == oferta.fontanero_id).first()
    if fontanero:
        data.fontanero_nombre = fontanero.nombre
        data.fontanero_valoracion = fontanero.valoracion
    data.num_postulantes = db.query(models.OfertaEmpleoPostulante).filter(
        models.OfertaEmpleoPostulante.oferta_id == oferta.id
    ).count()
    return data

@app.post("/ofertas-empleo", response_model=schemas.OfertaEmpleoRespuesta)
def crear_oferta_empleo(
    datos: schemas.OfertaEmpleoCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    if not fontanero:
        raise HTTPException(status_code=403, detail="Solo un profesional puede publicar una oferta de ayudante")
    oferta = models.OfertaEmpleo(
        fontanero_id=fontanero.id,
        gremio=fontanero.gremio,
        titulo=datos.titulo,
        descripcion=datos.descripcion,
        zona=datos.zona or fontanero.zona,
    )
    db.add(oferta)
    db.commit()
    db.refresh(oferta)
    return _oferta_empleo_con_datos(db, oferta)

@app.get("/ofertas-empleo", response_model=List[schemas.OfertaEmpleoRespuesta])
def listar_ofertas_empleo(
    gremio: Optional[str] = None,
    ciudad: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    query = db.query(models.OfertaEmpleo).filter(models.OfertaEmpleo.activa == True)
    if gremio:
        query = query.filter(models.OfertaEmpleo.gremio == gremio)
    if ciudad:
        query = query.filter(func.lower(models.OfertaEmpleo.zona) == ciudad.lower())
    ofertas = query.order_by(models.OfertaEmpleo.id.desc()).all()
    return [_oferta_empleo_con_datos(db, o) for o in ofertas]

@app.get("/ofertas-empleo/mias", response_model=List[schemas.OfertaEmpleoRespuesta])
def listar_mis_ofertas_empleo(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    if not fontanero:
        return []
    ofertas = db.query(models.OfertaEmpleo).filter(
        models.OfertaEmpleo.fontanero_id == fontanero.id
    ).order_by(models.OfertaEmpleo.id.desc()).all()
    return [_oferta_empleo_con_datos(db, o) for o in ofertas]

@app.put("/ofertas-empleo/{oferta_id}", response_model=schemas.OfertaEmpleoRespuesta)
def actualizar_oferta_empleo(
    oferta_id: int,
    datos: schemas.OfertaEmpleoActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    oferta = db.query(models.OfertaEmpleo).filter(
        models.OfertaEmpleo.id == oferta_id,
        models.OfertaEmpleo.fontanero_id == fontanero.id if fontanero else False,
    ).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    if datos.activa is not None:
        oferta.activa = datos.activa
    db.commit()
    db.refresh(oferta)
    return _oferta_empleo_con_datos(db, oferta)

@app.delete("/ofertas-empleo/{oferta_id}")
def eliminar_oferta_empleo(
    oferta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    oferta = db.query(models.OfertaEmpleo).filter(
        models.OfertaEmpleo.id == oferta_id,
        models.OfertaEmpleo.fontanero_id == fontanero.id if fontanero else False,
    ).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    db.query(models.OfertaEmpleoPostulante).filter(models.OfertaEmpleoPostulante.oferta_id == oferta_id).delete()
    db.delete(oferta)
    db.commit()
    return {"mensaje": "Oferta eliminada"}

@app.post("/ofertas-empleo/{oferta_id}/postular", response_model=schemas.OfertaEmpleoPostulanteRespuesta)
def postularse_oferta_empleo(
    oferta_id: int,
    datos: schemas.OfertaEmpleoPostularCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    oferta = db.query(models.OfertaEmpleo).filter(models.OfertaEmpleo.id == oferta_id).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    postulante = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    if not postulante:
        raise HTTPException(status_code=403, detail="Solo un profesional puede postularse")
    if postulante.id == oferta.fontanero_id:
        raise HTTPException(status_code=400, detail="No puedes postularte a tu propia oferta")
    existente = db.query(models.OfertaEmpleoPostulante).filter(
        models.OfertaEmpleoPostulante.oferta_id == oferta_id,
        models.OfertaEmpleoPostulante.fontanero_id == postulante.id,
    ).first()
    if existente:
        return schemas.OfertaEmpleoPostulanteRespuesta.model_validate(existente, from_attributes=True)
    postulacion = models.OfertaEmpleoPostulante(oferta_id=oferta_id, fontanero_id=postulante.id, mensaje=datos.mensaje)
    db.add(postulacion)
    if oferta.fontanero_id:
        dueño = db.query(models.Fontanero).filter(models.Fontanero.id == oferta.fontanero_id).first()
        if dueño and dueño.usuario_id:
            _crear_notificacion(db, dueño.usuario_id, "Nuevo postulante a tu oferta",
                                 f"{postulante.nombre} está interesado en \"{oferta.titulo}\"", "oferta_empleo_postulante", oferta_id)
    db.commit()
    db.refresh(postulacion)
    resultado = schemas.OfertaEmpleoPostulanteRespuesta.model_validate(postulacion, from_attributes=True)
    resultado.fontanero_nombre = postulante.nombre
    resultado.fontanero_telefono = postulante.telefono
    resultado.fontanero_valoracion = postulante.valoracion
    return resultado

@app.get("/ofertas-empleo/{oferta_id}/postulantes", response_model=List[schemas.OfertaEmpleoPostulanteRespuesta])
def listar_postulantes_oferta_empleo(
    oferta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    oferta = db.query(models.OfertaEmpleo).filter(
        models.OfertaEmpleo.id == oferta_id,
        models.OfertaEmpleo.fontanero_id == fontanero.id if fontanero else False,
    ).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    postulantes = db.query(models.OfertaEmpleoPostulante).filter(
        models.OfertaEmpleoPostulante.oferta_id == oferta_id
    ).order_by(models.OfertaEmpleoPostulante.id.desc()).all()
    resultado = []
    for p in postulantes:
        data = schemas.OfertaEmpleoPostulanteRespuesta.model_validate(p, from_attributes=True)
        postulante = db.query(models.Fontanero).filter(models.Fontanero.id == p.fontanero_id).first()
        if postulante:
            data.fontanero_nombre = postulante.nombre
            data.fontanero_telefono = postulante.telefono
            data.fontanero_valoracion = postulante.valoracion
        resultado.append(data)
    return resultado

@app.get("/servicios/{servicio_id}")
def ver_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = _verificar_participante_servicio(db, servicio_id, current_user)
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
    _verificar_email_profesional(db, fontanero_usuario_id)
    bloqueado = db.query(models.ListaNegraCliente).filter(
        models.ListaNegraCliente.cliente_id == servicio.cliente_id,
        models.ListaNegraCliente.fontanero_id == fontanero.id,
    ).first()
    if bloqueado:
        raise HTTPException(status_code=403, detail="Este cliente te ha bloqueado")
    if _num_servicios_comision_pendiente(db, fontanero) >= LIMITE_SERVICIOS_COMISION_PENDIENTE:
        raise HTTPException(
            status_code=400,
            detail=f"Tienes {LIMITE_SERVICIOS_COMISION_PENDIENTE} o más trabajos con la comisión sin liquidar. Paga la comisión pendiente antes de aceptar más.",
        )
    # UPDATE...WHERE atómico en vez de leer-y-luego-escribir: si dos fontaneros
    # aceptan el mismo servicio abierto a la vez, solo uno de los dos UPDATE
    # afecta una fila (el otro llega tarde y ve fontanero_id ya distinto al suyo).
    filas = db.query(models.Servicio).filter(
        models.Servicio.id == servicio_id,
        or_(models.Servicio.fontanero_id == None, models.Servicio.fontanero_id == fontanero.id),
    ).update({"fontanero_id": fontanero.id, "estado": "aceptado"}, synchronize_session=False)
    if filas == 0:
        db.rollback()
        raise HTTPException(status_code=400, detail="Este servicio ya fue asignado a otro profesional")
    db.commit()
    db.refresh(servicio)
    _crear_notificacion(db, servicio.cliente_id, "Servicio aceptado", f"Un fontanero ha aceptado tu solicitud de {servicio.tipo}", "servicio_aceptado", servicio.id)
    db.commit()
    _google_calendar_sync(db, servicio)
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
    precio: float = Field(gt=0)

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
    if servicio.estado in ("pago_pendiente", "pagado", "cancelado", "rechazado", "completado"):
        raise HTTPException(status_code=400, detail=f"No puedes cambiar el precio de un servicio en estado {servicio.estado}")
    _registrar_auditoria(db, servicio.id, "precio", servicio.precio, datos.precio, current_user)
    _registrar_auditoria(db, servicio.id, "estado", servicio.estado, "precio_enviado", current_user)
    servicio.precio = datos.precio
    servicio.estado = "precio_enviado"
    _crear_notificacion(db, servicio.cliente_id, "Precio recibido", f"El fontanero ha enviado un presupuesto de {datos.precio}€. Revísalo y acéptalo para continuar", "precio_enviado", servicio.id)
    db.commit()
    return {"mensaje": "Precio enviado al cliente", "precio": datos.precio}

@app.put("/servicios/{servicio_id}/precio/aceptar")
def aceptar_precio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El cliente acepta el precio propuesto: recién a partir de aquí el profesional
    puede ir a hacer el trabajo. Aceptar el precio NO es pagar — el pago solo se
    habilita cuando el profesional marca el trabajo como terminado (/completar)."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede aceptar el precio de este servicio")
    if servicio.estado != "precio_enviado":
        raise HTTPException(status_code=400, detail=f"No hay ningún precio pendiente de aceptar (estado: {servicio.estado})")
    _registrar_auditoria(db, servicio.id, "estado", servicio.estado, "precio_aceptado", current_user)
    servicio.estado = "precio_aceptado"
    if servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "✅ Precio aceptado", f"El cliente aceptó tu presupuesto de {servicio.precio}€. Ya puedes ir a hacer el trabajo", "precio_aceptado", servicio.id)
    db.commit()
    return {"mensaje": "Precio aceptado", "estado": servicio.estado}

@app.put("/servicios/{servicio_id}/precio/rechazar")
def rechazar_precio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El cliente rechaza el precio propuesto: no cancela el servicio, solo permite
    que el profesional proponga uno nuevo (por ejemplo tras seguir hablando en el chat)."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede rechazar el precio de este servicio")
    if servicio.estado != "precio_enviado":
        raise HTTPException(status_code=400, detail=f"No hay ningún precio pendiente de rechazar (estado: {servicio.estado})")
    precio_anterior = servicio.precio
    nuevo_estado = "pendiente" if servicio.es_consulta else "aceptado"
    _registrar_auditoria(db, servicio.id, "estado", servicio.estado, nuevo_estado, current_user)
    servicio.estado = nuevo_estado
    servicio.precio = None
    if servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "❌ Precio rechazado", f"El cliente rechazó tu presupuesto de {precio_anterior}€. Puedes proponer uno nuevo", "precio_rechazado", servicio.id)
    db.commit()
    return {"mensaje": "Precio rechazado", "estado": servicio.estado}

@app.put("/servicios/{servicio_id}/completar")
def completar_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El profesional marca el trabajo como terminado. Solo a partir de aquí el
    cliente puede pagar — antes de esto no debe existir ninguna forma de pagar
    ni de que el trabajo aparezca como cobrado."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="Este servicio no es tuyo")
    if servicio.estado not in ("precio_aceptado", "en_camino"):
        raise HTTPException(status_code=400, detail=f"No puedes marcar como terminado un servicio en estado {servicio.estado}")
    _registrar_auditoria(db, servicio.id, "estado", servicio.estado, "completado", current_user)
    servicio.estado = "completado"
    _crear_notificacion(db, servicio.cliente_id, "🏁 Trabajo terminado", f"{fontanero.nombre} marcó el trabajo como terminado. Ya puedes pagar", "trabajo_terminado", servicio.id)
    db.commit()
    return {"mensaje": "Servicio marcado como terminado", "estado": servicio.estado}

class PagoUpdate(BaseModel):
    metodo: Literal["efectivo", "bizum"]

@app.put("/servicios/{servicio_id}/pagar")
def confirmar_pago(
    servicio_id: int,
    datos: PagoUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    # Este endpoint SOLO registra la intención de pago para efectivo/bizum (que
    # quedan "pendientes" hasta que el fontanero confirme haberlos recibido, ver
    # /confirmar_efectivo y /confirmar_bizum). El pago con tarjeta NUNCA debe pasar
    # por aquí: se marca "pagado" únicamente en /stripe/verificar, que comprueba
    # de verdad contra Stripe que el cargo se hizo. Restringir
    # `metodo` con Literal evita que cualquier otro valor salte directo a "pagado"
    # sin haber pagado nada.
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede confirmar el pago de este servicio")
    if not servicio.precio:
        raise HTTPException(status_code=400, detail="El fontanero aún no ha enviado el precio")
    if servicio.estado == "pagado":
        raise HTTPException(status_code=400, detail="Este servicio ya está pagado")
    if servicio.estado != "completado":
        raise HTTPException(status_code=400, detail="El profesional aún no ha marcado el trabajo como terminado")
    nuevo_estado = "pago_pendiente"
    _registrar_auditoria(db, servicio.id, "estado", servicio.estado, nuevo_estado, current_user)
    servicio.estado = nuevo_estado
    servicio.metodo_pago = datos.metodo
    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None
    if fontanero_obj and fontanero_obj.usuario_id:
        if datos.metodo == "bizum":
            _crear_notificacion(db, fontanero_obj.usuario_id, "📱 ¿Has recibido el Bizum?",
                                 f"El cliente dice haber pagado {servicio.precio}€ por Bizum. Confírmalo en la app en cuanto lo compruebes.",
                                 "bizum_pendiente", servicio.id)
        else:
            _crear_notificacion(db, fontanero_obj.usuario_id, "💶 ¿Has recibido el efectivo?",
                                 f"El cliente dice haber pagado {servicio.precio}€ en efectivo. Confírmalo en la app en cuanto lo compruebes.",
                                 "pago_pendiente_efectivo", servicio.id)
    db.commit()
    return {"mensaje": "Pago registrado", "precio": servicio.precio, "metodo": datos.metodo}

def _confirmar_pago_directo(db: Session, servicio_id: int, current_user: dict, etiqueta: str):
    """Lógica compartida por /confirmar_efectivo y /confirmar_bizum: el fontanero
    confirma que recibió el dinero directamente del cliente (sin pasar por Stripe),
    así que la comisión de Multiservicios Provenza queda pendiente de liquidar."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="Este servicio no es tuyo")
    # UPDATE...WHERE atómico (mismo patrón que aceptar_servicio/aceptar_oferta):
    # si /confirmar_efectivo o /confirmar_bizum del mismo servicio llegan dos
    # veces a la vez (doble tap, reintento de red), solo una debe poder marcarlo
    # pagado y consumir la comisión/trabajo gratis; la otra debe ver que ya no
    # hay pago pendiente.
    filas = db.query(models.Servicio).filter(
        models.Servicio.id == servicio_id,
        models.Servicio.estado == "pago_pendiente",
    ).update({"estado": "pagado"}, synchronize_session=False)
    if filas == 0:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"No hay un pago pendiente de confirmar para este servicio (estado: {servicio.estado})")
    db.refresh(servicio)
    _registrar_auditoria(db, servicio.id, "estado", "pago_pendiente", "pagado", current_user)
    servicio.comision_aplicada = round((servicio.precio or 0) * _aplicar_comision_atomica(db, fontanero), 2)
    servicio.comision_liquidada = False
    _crear_notificacion(db, servicio.cliente_id, "Pago confirmado", f"El profesional confirmó haber recibido tu pago en {etiqueta}", "pago_confirmado", servicio_id)
    db.commit()
    return {"mensaje": f"{etiqueta} confirmado"}

@app.put("/servicios/{servicio_id}/confirmar_efectivo")
def confirmar_efectivo(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return _confirmar_pago_directo(db, servicio_id, current_user, "efectivo")

@app.put("/servicios/{servicio_id}/confirmar_bizum")
def confirmar_bizum(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return _confirmar_pago_directo(db, servicio_id, current_user, "Bizum")

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

    _registrar_auditoria(db, servicio.id, "estado", servicio.estado, "cancelado", current_user)
    servicio.estado = "cancelado"
    db.commit()
    _google_calendar_borrar(db, servicio)

    if es_cliente and servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Servicio cancelado", "El cliente ha cancelado la solicitud", "cancelado", servicio_id)
    elif es_fontanero:
        _crear_notificacion(db, servicio.cliente_id, "Servicio cancelado", "El profesional ha cancelado el servicio", "cancelado", servicio_id)
    if servicio.fontanero_id:
        _revisar_cancelaciones_repetidas(db, servicio.fontanero_id)
    db.commit()
    return {"mensaje": "Servicio cancelado"}

@app.put("/servicios/{servicio_id}/reprogramar", response_model=schemas.ServicioRespuesta)
def reprogramar_servicio(
    servicio_id: int,
    datos: schemas.ServicioReprogramar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Cambia la fecha/hora de una cita ya programada sin cancelarla: no toca el
    estado ni cuenta como cancelación en las estadísticas de ninguna de las partes."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    es_cliente = servicio.cliente_id == current_user["id"]
    fontanero_actual = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    es_fontanero = bool(fontanero_actual) and servicio.fontanero_id == fontanero_actual.id
    if not es_cliente and not es_fontanero:
        raise HTTPException(status_code=403, detail="No puedes reprogramar un servicio que no es tuyo")
    if servicio.estado in ["pagado", "completado", "cancelado", "rechazado", "en_camino"]:
        raise HTTPException(status_code=400, detail="Este servicio ya no se puede reprogramar")

    _registrar_auditoria(db, servicio.id, "fecha", servicio.fecha, datos.fecha, current_user)
    servicio.fecha = datos.fecha
    db.commit()
    _google_calendar_sync(db, servicio)

    if es_cliente and servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "📅 Cita reprogramada",
                                 "El cliente ha cambiado la fecha/hora del servicio", "reprogramado", servicio_id)
    elif es_fontanero:
        _crear_notificacion(db, servicio.cliente_id, "📅 Cita reprogramada",
                             "El profesional ha cambiado la fecha/hora del servicio", "reprogramado", servicio_id)
    db.commit()
    return schemas.ServicioRespuesta.from_orm_with_color(servicio)

@app.get("/clientes/{cliente_id}/servicios")
def ver_servicios_cliente(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes ver los servicios de otro cliente")
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

def _verificar_fontanero_propio(current_user: dict, fontanero_id: int) -> None:
    """En las rutas /fontaneros/{fontanero_id}/... el 'fontanero_id' del path es en
    realidad el usuario_id del profesional (así lo usa _resolver_fontanero y el resto
    de consultas de este módulo): solo el propio usuario puede gestionar sus datos."""
    if current_user["id"] != fontanero_id:
        raise HTTPException(status_code=403, detail="No puedes gestionar los datos de otro profesional")

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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
# Documentos de verificación de identidad (DNI, licencias...): fotos o PDF. Sin este
# filtro se podía subir cualquier extensión (p. ej. .html/.svg) que luego se sirve
# públicamente desde /uploads, con riesgo de contenido activo servido en el mismo origen.
ALLOWED_DOCUMENT_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
# El Content-Type de un multipart lo elige el cliente y no es de fiar, pero solo se
# usa para RECHAZAR (arriba). La extensión con la que se guarda en disco NUNCA debe
# salir del filename que manda el cliente (podría ser "x.html"/"x.svg" con
# Content-Type falseado a "image/jpeg" y servirse como contenido activo desde
# /uploads); se deriva siempre de este mapa fijo, ya validado contra la whitelist.
_EXT_POR_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "application/pdf": "pdf",
}

def _extension_segura(content_type: str, por_defecto: str = "jpg") -> str:
    return _EXT_POR_CONTENT_TYPE.get(content_type, por_defecto)

MAX_TAMANO_ARCHIVO = 15 * 1024 * 1024  # 15 MB

def _leer_archivo_limitado(archivo: UploadFile, max_bytes: int = MAX_TAMANO_ARCHIVO) -> bytes:
    contenido = archivo.file.read(max_bytes + 1)
    if len(contenido) > max_bytes:
        raise HTTPException(status_code=413, detail=f"El archivo supera el tamaño máximo permitido ({max_bytes // (1024*1024)} MB)")
    return contenido

def _marcar_fecha_hora(ruta: str):
    """Estampa fecha/hora (y la marca) en la foto, como prueba de que el trabajo
    se hizo cuando dice el profesional. Si algo falla, se deja la foto tal cual."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(ruta).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        texto = f"Multiservicios Provenza · {datetime.datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}"
        try:
            fuente = ImageFont.load_default(size=max(14, img.width // 40))
        except TypeError:
            fuente = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), texto, font=fuente)
        ancho_texto, alto_texto = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margen = 10
        x = max(0, img.width - ancho_texto - margen * 2)
        y = max(0, img.height - alto_texto - margen * 2)
        draw.rectangle([x - margen, y - margen, img.width, img.height], fill=(0, 0, 0, 140))
        draw.text((x, y), texto, font=fuente, fill=(255, 255, 255, 230))
        img.save(ruta)
    except Exception as e:
        print(f"[watermark] no se pudo marcar {ruta}: {e}")

@app.post("/servicios/{servicio_id}/imagenes", response_model=schemas.ImagenRespuesta)
def subir_imagen_servicio(
    servicio_id: int,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = _verificar_participante_servicio(db, servicio_id, current_user)
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = _extension_segura(archivo.content_type, "jpg")
    nombre_archivo = f"{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(_leer_archivo_limitado(archivo))
    _marcar_fecha_hora(ruta)
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
    _verificar_participante_servicio(db, servicio_id, current_user)
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
    _verificar_participante_servicio(db, servicio_id, current_user)
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
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes ver los chats de otro usuario")
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
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes registrar un token push para otro usuario")
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
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes eliminar un token push de otro usuario")
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
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes ver las notificaciones de otro usuario")
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
    if current_user["id"] != usuario_id:
        raise HTTPException(status_code=403, detail="No puedes modificar las notificaciones de otro usuario")
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = _extension_segura(archivo.content_type, "jpg")
    nombre_archivo = f"perfil_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(_leer_archivo_limitado(archivo))
    fontanero.foto_url = f"/uploads/{nombre_archivo}"
    db.commit()
    return {"foto_url": fontanero.foto_url}

@app.get("/fontaneros/{fontanero_id}/perfil", response_model=schemas.FontaneroRespuesta)
def ver_perfil_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    item = schemas.FontaneroRespuesta.model_validate(fontanero)
    # El código de referido solo le sirve al propio profesional (para compartirlo);
    # a cualquier otra persona que consulte este perfil público no se le expone.
    if current_user.get("id") != fontanero_id:
        item.codigo_referido = None
    usuario = db.query(models.Usuario).filter(models.Usuario.id == fontanero_id).first()
    item.miembro_desde = usuario.creado_en if usuario else None
    return item

@app.get("/fontaneros/{fontanero_id}/checklist-perfil")
def ver_checklist_perfil(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Checklist de perfil completo, para animar a completar el perfil antes del primer lead."""
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    tiene_servicios = db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.fontanero_id == fontanero.id, models.ServicioFontanero.activo == True
    ).count() > 0
    tiene_horario = db.query(models.HorarioBase).filter(
        models.HorarioBase.fontanero_id == fontanero.id
    ).count() > 0
    items = [
        {"clave": "foto", "etiqueta": "Foto de perfil", "hecho": bool(fontanero.foto_url)},
        {"clave": "descripcion", "etiqueta": "Descripción del negocio", "hecho": bool(fontanero.descripcion)},
        {"clave": "telefono", "etiqueta": "Teléfono de contacto", "hecho": bool(fontanero.telefono)},
        {"clave": "servicios", "etiqueta": "Al menos un servicio con precio", "hecho": tiene_servicios},
        {"clave": "horario", "etiqueta": "Horario de disponibilidad", "hecho": tiene_horario},
        {"clave": "verificacion", "etiqueta": "Documento de verificación subido", "hecho": db.query(models.DocumentoVerificacion).filter(models.DocumentoVerificacion.fontanero_id == fontanero.id).count() > 0},
    ]
    completados = sum(1 for i in items if i["hecho"])
    usuario = db.query(models.Usuario).filter(models.Usuario.id == fontanero_id).first()
    return {
        "items": items,
        "completados": completados,
        "total": len(items),
        "porcentaje": round(completados / len(items) * 100),
        "email_verificado": bool(usuario and usuario.email_verificado),
    }

# ─── VACACIONES ────────────────────────────────────────────────────────────────

@app.put("/fontaneros/{fontanero_id}/vacaciones")
def activar_vacaciones(
    fontanero_id: int,
    datos: schemas.VacacionesCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = _extension_segura(archivo.content_type, "jpg")
    nombre_archivo = f"galeria_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(_leer_archivo_limitado(archivo))
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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

class GaleriaDescripcionUpdate(BaseModel):
    descripcion: Optional[str] = None

@app.put("/fontaneros/{fontanero_id}/galeria/{foto_id}", response_model=schemas.GaleriaRespuesta)
def actualizar_descripcion_foto_galeria(
    fontanero_id: int,
    foto_id: int,
    datos: GaleriaDescripcionUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    foto.descripcion = datos.descripcion
    db.commit()
    db.refresh(foto)
    return foto

# ─── ESTADÍSTICAS ──────────────────────────────────────────────────────────────

@app.get("/fontaneros/{fontanero_id}/estadisticas", response_model=schemas.EstadisticasRespuesta)
def ver_estadisticas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    # Comisión real por servicio (ya considera trabajos gratis, comisión reducida por
    # referido, etc.) en vez de un 5% fijo que no coincide con lo realmente cobrado.
    ingresos_totales = sum(
        (s.precio or 0) - (s.comision_aplicada or 0) for s in servicios_pagados
    )

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

@app.get("/fontaneros/{fontanero_id}/comparativa-precio")
def ver_comparativa_precio(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Precio medio propio (servicios pagados) frente a la media del mismo
    gremio y zona, para que el profesional sepa si está caro o barato."""
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    precios_propios = [
        s.precio for s in db.query(models.Servicio).filter(
            models.Servicio.fontanero_id == fontanero.id,
            models.Servicio.estado == "pagado",
            models.Servicio.precio != None,
        ).all()
    ]
    precio_propio_medio = round(sum(precios_propios) / len(precios_propios), 2) if precios_propios else None

    precios_gremio = [
        s.precio for s in db.query(models.Servicio).join(
            models.Fontanero, models.Servicio.fontanero_id == models.Fontanero.id
        ).filter(
            models.Fontanero.gremio == fontanero.gremio,
            models.Fontanero.zona == fontanero.zona,
            models.Fontanero.id != fontanero.id,
            models.Servicio.estado == "pagado",
            models.Servicio.precio != None,
        ).all()
    ]
    precio_gremio_medio = round(sum(precios_gremio) / len(precios_gremio), 2) if precios_gremio else None

    diferencia_pct = None
    if precio_propio_medio and precio_gremio_medio:
        diferencia_pct = round((precio_propio_medio - precio_gremio_medio) / precio_gremio_medio * 100, 1)

    return {
        "precio_propio_medio": precio_propio_medio,
        "precio_gremio_zona_medio": precio_gremio_medio,
        "diferencia_pct": diferencia_pct,
        "muestra_gremio": len(precios_gremio),
    }

@app.get("/fontaneros/{fontanero_id}/resumen-fiscal")
def descargar_resumen_fiscal(
    fontanero_id: int,
    anio: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
    from fastapi.responses import Response
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        import io
    except ImportError:
        raise HTTPException(status_code=501, detail="Generación de PDF no disponible. Instala reportlab.")

    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    servicios_anio = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "pagado",
        func.extract("year", models.Servicio.creado_en) == anio,
    ).order_by(models.Servicio.creado_en).all()

    bruto = round(sum(s.precio or 0 for s in servicios_anio), 2)
    comisiones = round(sum(s.comision_aplicada or 0 for s in servicios_anio), 2)
    neto = round(bruto - comisiones, 2)

    por_mes = {}
    for s in servicios_anio:
        mes = s.creado_en.month if s.creado_en else 0
        por_mes[mes] = por_mes.get(mes, 0) + (s.precio or 0)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, h - 60, f"Resumen fiscal {anio} — Multiservicios Provenza")
    c.setFont("Helvetica", 12)
    c.drawString(50, h - 100, f"Profesional: {fontanero.nombre}")
    c.drawString(50, h - 120, f"Trabajos pagados en {anio}: {len(servicios_anio)}")
    c.drawString(50, h - 150, f"Total facturado (bruto): {bruto:.2f} EUR")
    c.drawString(50, h - 170, f"Comisión de la plataforma: {comisiones:.2f} EUR")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, h - 190, f"Ingreso neto: {neto:.2f} EUR")
    c.setFont("Helvetica", 11)
    y = h - 230
    c.drawString(50, y, "Desglose mensual (bruto):")
    y -= 20
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
             "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    for mes_num in range(1, 13):
        importe = por_mes.get(mes_num, 0)
        c.drawString(60, y, f"{meses[mes_num - 1]}: {importe:.2f} EUR")
        y -= 16
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, 50, "Documento informativo, no sustituye a la declaración oficial de la Agencia Tributaria.")
    c.save()
    buf.seek(0)
    return Response(
        content=buf.read(), media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=resumen_fiscal_{anio}.pdf"},
    )

# ─── BÚSQUEDA Y FILTROS ────────────────────────────────────────────────────────

@app.get("/buscar/fontaneros", response_model=List[schemas.FontaneroRespuesta])
def buscar_fontaneros(
    zona: Optional[str] = None,
    gremio: Optional[str] = None,
    disponible_24h: Optional[bool] = None,
    verificado: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
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
    fontaneros = q.order_by(nullslast(models.Fontanero.valoracion.desc())).all()
    resultado = []
    for f in fontaneros:
        item = schemas.FontaneroRespuesta.model_validate(f)
        item.codigo_referido = None  # solo le sirve al propio dueño (ver /fontaneros/{id}/perfil)
        resultado.append(item)
    return resultado

# ─── SEGUIMIENTO EN VIVO ───────────────────────────────────────────────────────

import math

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

UMBRAL_KM_CERCA = 1.2  # aprox. 5 minutos conduciendo en ciudad

def _avisar_si_esta_cerca(db: Session, fontanero):
    if fontanero.latitud is None or fontanero.longitud is None:
        return
    en_camino = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "en_camino",
        models.Servicio.aviso_proximidad_enviado == False,
        models.Servicio.latitud_cliente != None,
        models.Servicio.longitud_cliente != None,
    ).all()
    for servicio in en_camino:
        distancia = _haversine_km(fontanero.latitud, fontanero.longitud, servicio.latitud_cliente, servicio.longitud_cliente)
        if distancia <= UMBRAL_KM_CERCA:
            _crear_notificacion(db, servicio.cliente_id, "🚗 ¡Tu profesional está a punto de llegar!",
                                 f"{fontanero.nombre} está a unos 5 minutos", "proximidad", servicio.id)
            servicio.aviso_proximidad_enviado = True

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
    if servicio.estado != "precio_aceptado":
        raise HTTPException(status_code=400, detail=f"No puedes marcar en camino un servicio en estado {servicio.estado}")
    servicio.estado = "en_camino"
    _crear_notificacion(db, servicio.cliente_id, "🚗 Tu profesional va en camino",
                        f"{fontanero.nombre} ya está de camino. Puedes seguirlo en el mapa desde Mis Servicios",
                        "en_camino", servicio_id)
    db.commit()
    return {"mensaje": "Marcado en camino", "estado": servicio.estado}

@app.put("/servicios/{servicio_id}/llegue")
def marcar_llegada(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El profesional avisa manualmente que ya llegó (además del aviso automático
    por proximidad GPS): no cambia el estado, solo notifica al cliente al instante."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="Este servicio no es tuyo")
    if servicio.estado != "en_camino":
        raise HTTPException(status_code=400, detail=f"No puedes avisar la llegada en un servicio en estado {servicio.estado}")
    _crear_notificacion(db, servicio.cliente_id, "🔔 Tu profesional ha llegado",
                        f"{fontanero.nombre} está en tu domicilio", "llegada", servicio_id)
    db.commit()
    return {"mensaje": "Llegada notificada"}

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
        "latitud_cliente": servicio.latitud_cliente,
        "longitud_cliente": servicio.longitud_cliente,
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
    servicio = _verificar_participante_servicio(db, servicio_id, current_user)
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
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes modificar los favoritos de otro cliente")
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
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes modificar los favoritos de otro cliente")
    fav = db.query(models.Favorito).filter(
        models.Favorito.cliente_id == cliente_id,
        models.Favorito.fontanero_id == fontanero_id,
    ).first()
    if fav:
        db.delete(fav)
        db.commit()
    return {"mensaje": "Eliminado de favoritos"}

# ─── CUENTAS DE EMPRESA (EQUIPOS DE PROFESIONALES) ─────────────────────────────
# Un profesional puede convertirse en "empresa" (ponerle nombre comercial) e
# invitar a otros profesionales de su mismo gremio a su equipo. Los trabajos que
# recibe la empresa se pueden reasignar a cualquier empleado libre.

@app.put("/fontaneros/{fontanero_id}/empresa", response_model=schemas.FontaneroRespuesta)
def actualizar_empresa(
    fontanero_id: int,
    datos: schemas.EmpresaActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if fontanero.empresa_id:
        raise HTTPException(status_code=400, detail="Ya eres empleado de un equipo: no puedes crear tu propia empresa")
    fontanero.nombre_empresa = datos.nombre_empresa
    db.commit()
    db.refresh(fontanero)
    return fontanero

@app.post("/fontaneros/{fontanero_id}/equipo/invitar")
def invitar_a_equipo(
    fontanero_id: int,
    datos: schemas.EquipoInvitarCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
    dueño = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not dueño:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if not dueño.nombre_empresa:
        raise HTTPException(status_code=400, detail="Primero ponle un nombre a tu empresa antes de invitar")
    if dueño.empresa_id:
        raise HTTPException(status_code=400, detail="Eres empleado de otro equipo: no puedes tener el tuyo propio")
    usuario_invitado = db.query(models.Usuario).filter(models.Usuario.email == datos.email.lower()).first()
    if not usuario_invitado:
        raise HTTPException(status_code=404, detail="No existe ninguna cuenta con ese email")
    invitado = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario_invitado.id).first()
    if not invitado:
        raise HTTPException(status_code=404, detail="Esa cuenta no es de un profesional")
    if invitado.id == dueño.id:
        raise HTTPException(status_code=400, detail="No puedes invitarte a ti mismo")
    if invitado.gremio != dueño.gremio:
        raise HTTPException(status_code=400, detail="Solo puedes invitar a profesionales de tu mismo gremio")
    if invitado.empresa_id:
        raise HTTPException(status_code=400, detail="Ese profesional ya forma parte de un equipo")
    _crear_notificacion(db, usuario_invitado.id, "🤝 Invitación a un equipo",
                         f"{dueño.nombre_empresa} te invita a unirte a su equipo de {dueño.gremio}",
                         "invitacion_equipo", dueño.id)
    db.commit()
    return {"mensaje": "Invitación enviada"}

@app.put("/fontaneros/{fontanero_id}/equipo/aceptar")
def aceptar_invitacion_equipo(
    fontanero_id: int,
    datos: schemas.EquipoAceptarCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
    invitado = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not invitado:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    invitacion = db.query(models.Notificacion).filter(
        models.Notificacion.usuario_id == fontanero_id,
        models.Notificacion.tipo == "invitacion_equipo",
        models.Notificacion.referencia_id == datos.empresa_fontanero_id,
    ).order_by(models.Notificacion.id.desc()).first()
    if not invitacion:
        raise HTTPException(status_code=404, detail="No tienes ninguna invitación pendiente de ese equipo")
    dueño = db.query(models.Fontanero).filter(models.Fontanero.id == datos.empresa_fontanero_id).first()
    if not dueño:
        raise HTTPException(status_code=404, detail="La empresa ya no existe")
    if invitado.empresa_id:
        raise HTTPException(status_code=400, detail="Ya formas parte de un equipo")
    invitado.empresa_id = dueño.id
    db.commit()
    if dueño.usuario_id:
        _crear_notificacion(db, dueño.usuario_id, "✅ Se ha unido a tu equipo",
                             f"{invitado.nombre} ya forma parte de {dueño.nombre_empresa}", "equipo_miembro", invitado.id)
    db.commit()
    return {"mensaje": "Te has unido al equipo"}

@app.get("/fontaneros/{fontanero_id}/equipo", response_model=List[schemas.EquipoMiembroRespuesta])
def listar_equipo(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
    dueño = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not dueño:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.Fontanero).filter(models.Fontanero.empresa_id == dueño.id).all()

@app.delete("/fontaneros/{fontanero_id}/equipo/{empleado_id}")
def quitar_del_equipo(
    fontanero_id: int,
    empleado_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El dueño puede echar a un empleado, y un empleado puede salir del equipo él mismo."""
    _verificar_fontanero_propio(current_user, fontanero_id)
    quien_pide = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not quien_pide:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    empleado = db.query(models.Fontanero).filter(models.Fontanero.id == empleado_id).first()
    if not empleado or not empleado.empresa_id:
        raise HTTPException(status_code=404, detail="Ese profesional no está en un equipo")
    es_dueño = empleado.empresa_id == quien_pide.id
    es_el_mismo = empleado.id == quien_pide.id
    if not es_dueño and not es_el_mismo:
        raise HTTPException(status_code=403, detail="No puedes gestionar ese equipo")
    empleado.empresa_id = None
    db.commit()
    return {"mensaje": "Fuera del equipo"}

@app.put("/servicios/{servicio_id}/asignar-empleado", response_model=schemas.ServicioRespuesta)
def asignar_empleado(
    servicio_id: int,
    datos: schemas.ServicioAsignarEmpleado,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """El dueño de la empresa reparte un trabajo que le entró a él a uno de sus
    empleados libres, sin tener que hacerlo él mismo."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    dueño = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == current_user["id"]).first()
    if not dueño or servicio.fontanero_id != dueño.id:
        raise HTTPException(status_code=403, detail="Este servicio no es tuyo")
    if servicio.estado in ["pagado", "completado", "cancelado", "rechazado"]:
        raise HTTPException(status_code=400, detail="Este servicio ya no se puede reasignar")
    empleado = db.query(models.Fontanero).filter(
        models.Fontanero.id == datos.empleado_fontanero_id,
        models.Fontanero.empresa_id == dueño.id,
    ).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Ese profesional no es un empleado de tu equipo")
    servicio.fontanero_id = empleado.id
    db.commit()
    if empleado.usuario_id:
        _crear_notificacion(db, empleado.usuario_id, "📋 Nuevo trabajo asignado",
                             f"{dueño.nombre_empresa or dueño.nombre} te ha asignado un trabajo de {servicio.tipo}",
                             "trabajo_asignado", servicio_id)
    _crear_notificacion(db, servicio.cliente_id, "Profesional asignado",
                         f"{empleado.nombre} se encargará de tu servicio", "empleado_asignado", servicio_id)
    db.commit()
    return schemas.ServicioRespuesta.from_orm_with_color(servicio)

# ─── LISTA NEGRA PERSONAL DEL CLIENTE ──────────────────────────────────────────
# Distinta del veto del admin: aquí el cliente simplemente deja de ver/recibir
# avisos de un profesional concreto, sin que eso afecte a los demás clientes.

@app.get("/clientes/{cliente_id}/lista-negra")
def ver_lista_negra(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes ver la lista negra de otro cliente")
    entradas = db.query(models.ListaNegraCliente).filter(models.ListaNegraCliente.cliente_id == cliente_id).all()
    resultado = []
    for e in entradas:
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == e.fontanero_id).first()
        resultado.append({"fontanero_id": e.fontanero_id, "fontanero_nombre": fontanero.nombre if fontanero else None})
    return resultado

@app.post("/clientes/{cliente_id}/lista-negra/{fontanero_id}")
def agregar_a_lista_negra(
    cliente_id: int,
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes modificar la lista negra de otro cliente")
    existe = db.query(models.ListaNegraCliente).filter(
        models.ListaNegraCliente.cliente_id == cliente_id,
        models.ListaNegraCliente.fontanero_id == fontanero_id,
    ).first()
    if existe:
        return {"mensaje": "Ya no verás a este profesional"}
    db.add(models.ListaNegraCliente(cliente_id=cliente_id, fontanero_id=fontanero_id))
    db.commit()
    return {"mensaje": "Ya no verás a este profesional"}

@app.delete("/clientes/{cliente_id}/lista-negra/{fontanero_id}")
def quitar_de_lista_negra(
    cliente_id: int,
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes modificar la lista negra de otro cliente")
    entrada = db.query(models.ListaNegraCliente).filter(
        models.ListaNegraCliente.cliente_id == cliente_id,
        models.ListaNegraCliente.fontanero_id == fontanero_id,
    ).first()
    if entrada:
        db.delete(entrada)
        db.commit()
    return {"mensaje": "Profesional restaurado"}

@app.get("/clientes/{cliente_id}/favoritos", response_model=List[schemas.FontaneroRespuesta])
def listar_favoritos(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes ver los favoritos de otro cliente")
    favs = db.query(models.Favorito).filter(models.Favorito.cliente_id == cliente_id).all()
    preferente_por_fontanero = {f.fontanero_id: f.preferente for f in favs}
    ids = list(preferente_por_fontanero.keys())
    fontaneros = db.query(models.Fontanero).filter(models.Fontanero.id.in_(ids)).all()
    resultado = []
    for f in fontaneros:
        item = schemas.FontaneroRespuesta.model_validate(f)
        item.codigo_referido = None  # solo le sirve al propio dueño (ver /fontaneros/{id}/perfil)
        item.favorito_preferente = bool(preferente_por_fontanero.get(f.id))
        resultado.append(item)
    return resultado

@app.put("/clientes/{cliente_id}/favoritos/{fontanero_id}/preferente")
def marcar_favorito_preferente(
    cliente_id: int,
    fontanero_id: int,
    datos: schemas.FavoritoPreferenteActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Marca un favorito como "profesional de confianza" para su gremio: las próximas
    solicitudes no urgentes de ese gremio (sin elegir profesional a mano) le avisan
    primero a él, con una ventana de prioridad antes de abrirse al resto del mercado."""
    if current_user["id"] != cliente_id:
        raise HTTPException(status_code=403, detail="No puedes modificar los favoritos de otro cliente")
    fav = db.query(models.Favorito).filter(
        models.Favorito.cliente_id == cliente_id,
        models.Favorito.fontanero_id == fontanero_id,
    ).first()
    if not fav:
        raise HTTPException(status_code=404, detail="Ese profesional no está en tus favoritos")
    if datos.preferente:
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
        if not fontanero:
            raise HTTPException(status_code=404, detail="Profesional no encontrado")
        # Solo puede haber un preferente por gremio: se desmarca cualquier otro.
        otros_favoritos = db.query(models.Favorito).filter(
            models.Favorito.cliente_id == cliente_id,
            models.Favorito.preferente == True,
            models.Favorito.fontanero_id != fontanero_id,
        ).all()
        for otro in otros_favoritos:
            otro_fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == otro.fontanero_id).first()
            if otro_fontanero and otro_fontanero.gremio == fontanero.gremio:
                otro.preferente = False
    fav.preferente = datos.preferente
    db.commit()
    return {"mensaje": "Profesional de confianza actualizado" if datos.preferente else "Ya no es tu profesional de confianza"}

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
    _verificar_email_profesional(db, current_user["id"])
    if servicio.estado != "pendiente":
        raise HTTPException(status_code=400, detail="Este servicio ya no admite nuevas ofertas")
    if servicio.fontanero_id not in (None, fontanero.id):
        raise HTTPException(status_code=403, detail="Este servicio ya está dirigido a otro profesional")
    bloqueado = db.query(models.ListaNegraCliente).filter(
        models.ListaNegraCliente.cliente_id == servicio.cliente_id,
        models.ListaNegraCliente.fontanero_id == fontanero.id,
    ).first()
    if bloqueado:
        raise HTTPException(status_code=403, detail="Este cliente te ha bloqueado")

    if datos.materiales is not None and datos.mano_obra is not None:
        precio_final = datos.materiales + datos.mano_obra
    elif datos.precio is not None:
        precio_final = datos.precio
    else:
        raise HTTPException(status_code=422, detail="Falta el precio, o el desglose de materiales y mano de obra")
    if precio_final <= 0:
        raise HTTPException(status_code=422, detail="El precio debe ser mayor que cero")

    existente = db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.fontanero_id == fontanero.id,
    ).first()
    if existente:
        existente.precio = precio_final
        existente.materiales = datos.materiales
        existente.mano_obra = datos.mano_obra
        existente.mensaje = datos.mensaje
        db.commit()
        db.refresh(existente)
        return existente

    ofertas_activas = db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.estado == "pendiente",
    ).count()
    if ofertas_activas >= CUPO_MAXIMO_OFERTAS:
        raise HTTPException(status_code=409, detail="Ya hay suficientes profesionales interesados en este servicio")

    oferta = models.Oferta(
        servicio_id=servicio_id, fontanero_id=fontanero.id,
        precio=precio_final, materiales=datos.materiales, mano_obra=datos.mano_obra,
        mensaje=datos.mensaje,
    )
    db.add(oferta)
    _crear_notificacion(db, servicio.cliente_id, "Nueva oferta recibida", f"Un profesional ofrece realizar el servicio por {precio_final}€", "oferta", servicio_id)
    db.commit()
    db.refresh(oferta)
    return oferta

@app.get("/servicios/{servicio_id}/ofertas", response_model=List[schemas.OfertaRespuesta])
def listar_ofertas(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente dueño del servicio puede ver sus ofertas")
    bloqueados = db.query(models.ListaNegraCliente.fontanero_id).filter(
        models.ListaNegraCliente.cliente_id == current_user["id"]
    )
    ofertas = db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.estado == "pendiente",
        ~models.Oferta.fontanero_id.in_(bloqueados),
    ).order_by(models.Oferta.precio).all()
    return [_enriquecer_oferta(db, o, incluir_fontanero=True) for o in ofertas]

@app.get("/fontaneros/{fontanero_id}/ofertas", response_model=List[schemas.OfertaRespuesta])
def listar_mis_ofertas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    # UPDATE...WHERE atómico en vez de leer-y-luego-escribir: evita que dos ofertas
    # del mismo servicio se acepten a la vez (mismo patrón que aceptar_servicio).
    filas = db.query(models.Servicio).filter(
        models.Servicio.id == servicio_id,
        models.Servicio.estado == "pendiente",
    ).update({"fontanero_id": oferta.fontanero_id, "precio": oferta.precio, "estado": "aceptado"}, synchronize_session=False)
    if filas == 0:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Este servicio ya no admite aceptar ofertas (estado: {servicio.estado})")
    db.refresh(servicio)
    oferta.estado = "aceptada"
    db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.id != oferta_id,
    ).update({"estado": "rechazada"})
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == oferta.fontanero_id).first()
    if fontanero and fontanero.usuario_id:
        _crear_notificacion(db, fontanero.usuario_id, "¡Tu oferta fue aceptada!", f"El cliente aceptó tu oferta de {oferta.precio}€", "oferta_aceptada", servicio_id)
    db.commit()
    _google_calendar_sync(db, servicio)
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
        _revisar_resenas_bajas(db, fontanero.id)
    db.commit()
    db.refresh(resena)
    return resena

@app.get("/fontaneros/{fontanero_id}/resenas", response_model=List[schemas.ResenaRespuesta])
def ver_resenas(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    resenas = db.query(models.Resena).filter(
        models.Resena.fontanero_id == fontanero.id
    ).order_by(models.Resena.creado_en.desc()).all()
    resultado = []
    for r in resenas:
        item = schemas.ResenaRespuesta.model_validate(r, from_attributes=True)
        cliente = db.query(models.Usuario).filter(models.Usuario.id == r.cliente_id).first()
        item.cliente_nombre = cliente.nombre if cliente else "Cliente"
        resultado.append(item)
    return resultado

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
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if datos.servicio_id is not None:
        # Sin esto, cualquier fontanero podía enlazar una cita a un servicio_id ajeno
        # (adivinable, son IDs secuenciales) y ver_ruta_hoy le devolvía la ubicación
        # GPS del cliente de ese servicio aunque no tuviera ninguna relación con él.
        servicio_vinculado = db.query(models.Servicio).filter(
            models.Servicio.id == datos.servicio_id,
            models.Servicio.fontanero_id == fontanero.id,
        ).first()
        if not servicio_vinculado:
            raise HTTPException(status_code=403, detail="Ese servicio no es tuyo")
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

@app.get("/fontaneros/{fontanero_id}/ruta-hoy")
def ver_ruta_hoy(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Citas de hoy ordenadas por cercanía geográfica, empezando desde la posición
    actual del profesional (algoritmo del vecino más cercano). Junta tanto las citas
    del calendario interno como los servicios con fecha programada para hoy."""
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    hoy = datetime.datetime.utcnow().date()

    pendientes = []
    vistos_servicio_id = set()

    citas_hoy = db.query(models.Cita).filter(
        models.Cita.fontanero_id == fontanero.id,
        func.date(models.Cita.fecha_inicio) == hoy,
    ).all()
    for cita in citas_hoy:
        servicio = db.query(models.Servicio).filter(models.Servicio.id == cita.servicio_id).first() if cita.servicio_id else None
        pendientes.append({
            "id": f"cita-{cita.id}",
            "titulo": cita.titulo,
            "fecha_inicio": cita.fecha_inicio,
            "servicio_id": cita.servicio_id,
            "latitud": servicio.latitud_cliente if servicio else None,
            "longitud": servicio.longitud_cliente if servicio else None,
            "tiene_ubicacion": bool(servicio and servicio.latitud_cliente is not None),
        })
        if cita.servicio_id:
            vistos_servicio_id.add(cita.servicio_id)

    servicios_hoy = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado.in_(["aceptado", "precio_enviado", "pago_pendiente", "en_camino"]),
        models.Servicio.fecha != None,
        func.date(models.Servicio.fecha) == hoy,
    ).all()
    for servicio in servicios_hoy:
        if servicio.id in vistos_servicio_id:
            continue
        pendientes.append({
            "id": f"servicio-{servicio.id}",
            "titulo": servicio.tipo,
            "fecha_inicio": servicio.fecha,
            "servicio_id": servicio.id,
            "latitud": servicio.latitud_cliente,
            "longitud": servicio.longitud_cliente,
            "tiene_ubicacion": servicio.latitud_cliente is not None,
        })

    con_ubicacion = [c for c in pendientes if c["tiene_ubicacion"]]
    sin_ubicacion = [c for c in pendientes if not c["tiene_ubicacion"]]

    ruta = []
    if con_ubicacion:
        lat_actual = fontanero.latitud
        lon_actual = fontanero.longitud
        restantes = con_ubicacion[:]
        while restantes:
            if lat_actual is not None and lon_actual is not None:
                restantes.sort(key=lambda c: _haversine_km(lat_actual, lon_actual, c["latitud"], c["longitud"]))
            siguiente = restantes.pop(0)
            ruta.append(siguiente)
            lat_actual, lon_actual = siguiente["latitud"], siguiente["longitud"]

    sin_ubicacion.sort(key=lambda c: c["fecha_inicio"] or datetime.datetime.min)
    return {"con_ubicacion": ruta, "sin_ubicacion": sin_ubicacion}

@app.get("/fontaneros/{fontanero_id}/citas", response_model=List[schemas.CitaRespuesta])
def ver_citas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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

# ─── GOOGLE CALENDAR (sincronización de citas confirmadas) ────────────────────
# Requiere GOOGLE_CLIENT_ID (ya usado por "Sign in with Google") y además
# GOOGLE_CLIENT_SECRET, con la Calendar API habilitada en el mismo proyecto de
# Google Cloud Console, y la redirect URI de abajo dada de alta ahí.

GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")


def _google_calendar_redirect_uri() -> str:
    base_url = os.getenv("BACKEND_URL", "https://fontap-backend-production.up.railway.app")
    return f"{base_url}/auth/google/calendar/callback"


def _google_login_redirect_uri() -> str:
    base_url = os.getenv("BACKEND_URL", "https://fontap-backend-production.up.railway.app")
    return f"{base_url}/auth/google/login/callback"


@app.get("/auth/google/login/callback")
def google_login_callback():
    """Google solo admite un redirect_uri con dominio real (rechaza fontap:///),
    así que 'Sign in with Google' vuelve primero aquí. Google deja el id_token
    en el fragmento de la URL (#...), que nunca llega al servidor; esta página
    solo lo lee en el navegador y lo reenvía al esquema propio de la app, que
    expo-web-browser sí puede capturar (solo funciona en un build real, no en
    Expo Go)."""
    from fastapi.responses import Response
    html = (
        "<!doctype html><html><body style='font-family:sans-serif;text-align:center;padding:60px 20px;'>"
        "<p>Conectando con Google…</p>"
        "<script>window.location.replace('fontap:///?' + window.location.hash.substring(1));</script>"
        "</body></html>"
    )
    return Response(content=html, media_type="text/html")


def _google_calendar_access_token(fontanero) -> Optional[str]:
    if not (fontanero and fontanero.google_calendar_refresh_token and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        return None
    try:
        resp = _http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": fontanero.google_calendar_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("access_token")
    except Exception:
        return None


def _google_calendar_evento_body(servicio) -> dict:
    inicio = servicio.fecha
    fin = inicio + datetime.timedelta(hours=2)
    return {
        "summary": f"{servicio.tipo or 'Servicio'} · Multiservicios Provenza",
        "description": f"Solicitud #{servicio.id}" + (f"\n{servicio.descripcion}" if servicio.descripcion else ""),
        "start": {"dateTime": inicio.isoformat(), "timeZone": "Europe/Madrid"},
        "end": {"dateTime": fin.isoformat(), "timeZone": "Europe/Madrid"},
    }


def _google_calendar_sync(db: Session, servicio) -> None:
    """Best-effort: crea o actualiza el evento en el Google Calendar del profesional.
    Si algo falla (token revocado, sin red, etc.) no interrumpe el flujo principal."""
    if not servicio.fontanero_id or not servicio.fecha:
        return
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
    if not fontanero or not fontanero.google_calendar_conectado:
        return
    access_token = _google_calendar_access_token(fontanero)
    if not access_token:
        return
    headers = {"Authorization": f"Bearer {access_token}"}
    body = _google_calendar_evento_body(servicio)
    try:
        if servicio.google_event_id:
            resp = _http.patch(
                f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{servicio.google_event_id}",
                json=body, headers=headers, timeout=8,
            )
        else:
            resp = _http.post(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                json=body, headers=headers, timeout=8,
            )
            if resp.status_code in (200, 201):
                servicio.google_event_id = resp.json().get("id")
                db.commit()
    except Exception:
        pass


def _google_calendar_borrar(db: Session, servicio) -> None:
    if not servicio.google_event_id or not servicio.fontanero_id:
        return
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
    access_token = _google_calendar_access_token(fontanero)
    if not access_token:
        return
    try:
        _http.delete(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{servicio.google_event_id}",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=8,
        )
    except Exception:
        pass
    servicio.google_event_id = None
    db.commit()


@app.get("/fontaneros/{fontanero_id}/google-calendar/conectar")
def google_calendar_conectar(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["id"] != fontanero_id:
        raise HTTPException(status_code=403, detail="Solo puedes conectar tu propio calendario")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=400, detail="Google Calendar no está configurado en el servidor")
    from urllib.parse import urlencode
    # Token de estado aleatorio ligado al usuario que inicia el flujo: el callback
    # nunca debe confiar en un usuario_id que llega directo por query string (eso
    # permitiría a un atacante completar su propio consentimiento de Google y
    # enlazar su refresh_token a la cuenta de otro profesional). Igual que
    # VerificacionEmail/PasswordReset: token corto de un solo uso con caducidad.
    state_token = secrets.token_urlsafe(24)
    db.add(models.EstadoOAuth(
        usuario_id=current_user["id"], token=state_token,
        expira=datetime.datetime.utcnow() + datetime.timedelta(minutes=10),
    ))
    db.commit()
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _google_calendar_redirect_uri(),
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "https://www.googleapis.com/auth/calendar.events",
        "state": state_token,
    }
    return {"url": f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"}


@app.get("/auth/google/calendar/callback")
def google_calendar_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None, db: Session = Depends(get_db)):
    from fastapi.responses import Response

    def _pagina(mensaje: str) -> Response:
        return Response(
            content=f"<html><body style='font-family:sans-serif;text-align:center;padding:60px 20px;'><h2>{mensaje}</h2><p>Ya puedes volver a la app.</p></body></html>",
            media_type="text/html",
        )

    if error or not code or not state:
        return _pagina("❌ No se pudo conectar Google Calendar")
    estado = db.query(models.EstadoOAuth).filter(
        models.EstadoOAuth.token == state,
        models.EstadoOAuth.usado == False,
    ).order_by(models.EstadoOAuth.id.desc()).first()
    if not estado or estado.expira < datetime.datetime.utcnow():
        return _pagina("❌ El enlace de conexión caducó o no es válido. Vuelve a intentarlo desde la app")
    estado.usado = True
    db.commit()
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == estado.usuario_id).first()
    if not fontanero:
        return _pagina("❌ Profesional no encontrado")
    try:
        resp = _http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _google_calendar_redirect_uri(),
            },
            timeout=8,
        )
        datos_token = resp.json()
    except Exception:
        return _pagina("❌ No se pudo completar la conexión con Google")
    refresh_token = datos_token.get("refresh_token")
    if not refresh_token:
        return _pagina("⚠️ Google no devolvió permiso permanente. Desconecta el acceso de Multiservicios Provenza en tu cuenta de Google y vuelve a intentarlo")
    fontanero.google_calendar_refresh_token = refresh_token
    fontanero.google_calendar_conectado = True
    db.commit()
    return _pagina("✅ Google Calendar conectado")


@app.delete("/fontaneros/{fontanero_id}/google-calendar")
def google_calendar_desconectar(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    if current_user["id"] != fontanero_id:
        raise HTTPException(status_code=403, detail="Solo puedes desconectar tu propio calendario")
    fontanero.google_calendar_refresh_token = None
    fontanero.google_calendar_conectado = False
    db.commit()
    return {"mensaje": "Google Calendar desconectado"}

# ─── HISTORIAL DE PAGOS FONTANERO ─────────────────────────────────────────────

@app.get("/fontaneros/{fontanero_id}/pagos")
def historial_pagos(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_fontanero_propio(current_user, fontanero_id)
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
        # Comisión real aplicada a este servicio (no un 5% fijo, que no refleja
        # trabajos gratis ni la comisión reducida por referido).
        comision = round(s.comision_aplicada or 0, 2)
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

    servicio = _verificar_participante_servicio(db, servicio_id, current_user)
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
    if servicio.estado == "pagado":
        raise HTTPException(status_code=400, detail="Este servicio ya está pagado")
    if servicio.estado != "completado":
        raise HTTPException(status_code=400, detail="El profesional aún no ha marcado el trabajo como terminado")

    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None

    # El pago con tarjeta reparte el dinero automáticamente al profesional (menos la
    # comisión) vía Stripe Connect. Si no tiene cuenta conectada y activa, el dinero
    # se quedaría en la cuenta de Multiservicios Provenza sin ninguna forma automática
    # de llegarle, así que no se permite cobrar por tarjeta hasta que la conecte.
    if not fontanero_obj or not fontanero_obj.stripe_account_id:
        raise HTTPException(
            status_code=400,
            detail="Este profesional todavía no ha conectado su cuenta de cobro. Pídele que pulse \"Conectar\" en su panel, o paga en efectivo/Bizum mientras tanto.",
        )
    cuenta_stripe = stripe.Account.retrieve(fontanero_obj.stripe_account_id)
    if not cuenta_stripe.charges_enabled:
        raise HTTPException(
            status_code=400,
            detail="Este profesional está terminando de configurar su cuenta de cobro. Prueba de nuevo en unos minutos, o paga en efectivo/Bizum mientras tanto.",
        )

    comision_centavos = round(servicio.precio * 100 * _tasa_comision(fontanero_obj))

    checkout_kwargs = dict(
        mode="payment",
        automatic_payment_methods={"enabled": True},
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
        payment_intent_data={
            "application_fee_amount": comision_centavos,
            "transfer_data": {"destination": fontanero_obj.stripe_account_id},
        },
    )
    try:
        session = stripe.checkout.Session.create(**checkout_kwargs)
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo iniciar el pago con tarjeta. Inténtalo de nuevo.")
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
    es_cliente = servicio.cliente_id == current_user["id"]
    fontanero_actual = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    es_fontanero = bool(fontanero_actual) and servicio.fontanero_id == fontanero_actual.id
    if not es_cliente and not es_fontanero:
        raise HTTPException(status_code=403, detail="No puedes verificar el pago de un servicio que no es tuyo")

    session = stripe.checkout.Session.retrieve(servicio.stripe_payment_intent)
    if session.payment_status != "paid":
        return {"pagado": False}
    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None
    # UPDATE...WHERE atómico: evita que dos llamadas concurrentes a /stripe/verificar
    # del mismo servicio (reintento de red, doble navegación de vuelta del navegador)
    # apliquen la comisión y consuman el trabajo gratis dos veces.
    filas = db.query(models.Servicio).filter(
        models.Servicio.id == servicio_id,
        models.Servicio.estado != "pagado",
    ).update({"estado": "pagado", "metodo_pago": "stripe"}, synchronize_session=False)
    if filas > 0:
        db.refresh(servicio)
        servicio.comision_aplicada = round((servicio.precio or 0) * _aplicar_comision_atomica(db, fontanero_obj), 2)
        servicio.comision_liquidada = True  # Stripe ya retuvo/repartió la comisión al cobrar
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Pago recibido", f"Pago de {servicio.precio}€ confirmado por Stripe", "pago_recibido", servicio_id)
        db.commit()
    return {"pagado": True}

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
    if fontanero_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="No puedes ver el estado de Stripe de otro fontanero")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if not fontanero.stripe_account_id:
        return {"conectado": False, "cobros_activos": False}
    stripe = _stripe_o_501()
    cuenta = stripe.Account.retrieve(fontanero.stripe_account_id)
    return {"conectado": True, "cobros_activos": bool(cuenta.charges_enabled)}

# ─── COMISIÓN PENDIENTE (pagos en efectivo/Bizum que no pasan por Stripe) ─────
# Bizum/transferencia de un profesional hacia la propia plataforma no se puede
# verificar automáticamente (no hay integración real): el profesional ve el
# importe y el destino, y un administrador la marca como pagada a mano tras
# comprobarlo en el banco (ver /admin/fontaneros/{id}/comision-pendiente/marcar-pagada).
COMISION_BIZUM_TELEFONO = os.getenv("COMISION_BIZUM_TELEFONO", "")
COMISION_TRANSFERENCIA_IBAN = os.getenv("COMISION_TRANSFERENCIA_IBAN", "")

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
    return {
        "total": total,
        "servicios": [s.id for s in pendientes],
        "num_servicios": len(pendientes),
        "limite_servicios": LIMITE_SERVICIOS_COMISION_PENDIENTE,
    }

def _instrucciones_comision(fontanero, total: float, telefono_o_iban: str, articulo_metodo: str, etiqueta_metodo: str) -> dict:
    return {
        "importe": total,
        "concepto": fontanero.nombre,
        "destino": telefono_o_iban,
        "instrucciones": [
            f"1. Abre tu app del banco y haz {articulo_metodo} {etiqueta_metodo} de {total}€.",
            f"2. Destino: {telefono_o_iban or 'consulta con soporte, no está configurado'}.",
            f"3. En el concepto, pon tu nombre completo: \"{fontanero.nombre}\".",
            "4. En cuanto lo recibamos, un administrador marcará tu comisión como pagada.",
        ],
    }

@app.get("/fontaneros/{fontanero_id}/comision-pendiente/bizum")
def comision_pendiente_bizum(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if fontanero_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="No puedes ver la comisión de otro fontanero")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    total = round(sum(
        s.comision_aplicada or 0 for s in db.query(models.Servicio).filter(
            models.Servicio.fontanero_id == fontanero.id,
            models.Servicio.comision_liquidada == False,
            models.Servicio.comision_aplicada != None,
        ).all()
    ), 2)
    if total <= 0:
        raise HTTPException(status_code=400, detail="No tienes comisión pendiente")
    return _instrucciones_comision(fontanero, total, COMISION_BIZUM_TELEFONO, "un", "Bizum")

@app.get("/fontaneros/{fontanero_id}/comision-pendiente/transferencia")
def comision_pendiente_transferencia(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if fontanero_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="No puedes ver la comisión de otro fontanero")
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    total = round(sum(
        s.comision_aplicada or 0 for s in db.query(models.Servicio).filter(
            models.Servicio.fontanero_id == fontanero.id,
            models.Servicio.comision_liquidada == False,
            models.Servicio.comision_aplicada != None,
        ).all()
    ), 2)
    if total <= 0:
        raise HTTPException(status_code=400, detail="No tienes comisión pendiente")
    return _instrucciones_comision(fontanero, total, COMISION_TRANSFERENCIA_IBAN, "una", "transferencia")

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
        automatic_payment_methods={"enabled": True},
        line_items=[{
            "price_data": {
                "currency": "eur",
                "product_data": {"name": "Multiservicios Provenza — comisión pendiente"},
                "unit_amount": round(total * 100),
            },
            "quantity": 1,
        }],
        metadata={
            "fontanero_id": str(fontanero_id),
            "tipo": "comision_pendiente",
            # Fija qué servicios cubre este cobro exacto: si el fontanero liquida más
            # trabajos en efectivo/Bizum mientras este checkout está pendiente de pago,
            # /verificar no debe darlos por pagados también sin que medie dinero real.
            "servicio_ids": ",".join(str(s.id) for s in pendientes),
        },
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
        ids_pagados_raw = (session.metadata or {}).get("servicio_ids", "")
        ids_pagados = [int(sid) for sid in ids_pagados_raw.split(",") if sid.strip().isdigit()]
        # Solo liquida los servicios que formaban parte de este cobro concreto (guardados
        # en el metadata al crear el checkout): así, si el fontanero acumuló más comisión
        # pendiente después de crear este checkout, esa comisión nueva sigue pendiente.
        if ids_pagados:
            db.query(models.Servicio).filter(
                models.Servicio.fontanero_id == fontanero.id,
                models.Servicio.id.in_(ids_pagados),
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
    servicio = _verificar_participante_servicio(db, servicio_id, current_user)
    if not servicio.precio:
        raise HTTPException(status_code=400, detail="Servicio no encontrado o sin precio")
    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None
    usuario_fontanero = db.query(models.Usuario).filter(models.Usuario.id == fontanero_obj.usuario_id).first() if fontanero_obj else None
    return {
        "importe": servicio.precio,
        "concepto": f"Multiservicios Provenza servicio #{servicio_id}",
        "telefono_destino": usuario_fontanero.telefono if usuario_fontanero else "Consultar con el profesional",
        "instrucciones": [
            "1. Abre tu app bancaria y selecciona Bizum",
            f"2. Envía {servicio.precio}€ al teléfono del profesional",
            f"3. Añade el concepto: Multiservicios Provenza servicio #{servicio_id}",
            "4. Vuelve a la app y confirma el pago — el profesional recibirá un aviso para confirmar que lo recibió",
        ],
    }

# ─── INMUEBLES (ADMIN FINCAS) ─────────────────────────────────────────────────

@app.post("/inmuebles", response_model=schemas.InmuebleRespuesta)
def crear_inmueble(
    datos: schemas.InmuebleCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if current_user["tipo"] != "administrador_fincas":
        raise HTTPException(status_code=403, detail="Solo un administrador de fincas puede registrar inmuebles")
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
    _verificar_fontanero_propio(current_user, fontanero_id)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if archivo.content_type not in ALLOWED_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido. Sube una foto (JPEG/PNG/WEBP) o un PDF")
    ext = _extension_segura(archivo.content_type, "pdf")
    nombre_archivo = f"doc_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(_leer_archivo_limitado(archivo))
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
    _verificar_fontanero_propio(current_user, fontanero_id)
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
    # Comisión real cobrada por servicio, no un 5% fijo (que no refleja trabajos
    # gratis ni la comisión reducida por referido).
    ingresos = sum(s.comision_aplicada or 0 for s in pagados)
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

@app.delete("/admin/usuarios/{usuario_id}")
def admin_eliminar_usuario(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """A diferencia de vetar (reversible, solo oculta), esto borra los datos
    personales del usuario de forma permanente (misma anonimización RGPD que
    cuando un usuario elimina su propia cuenta)."""
    _verificar_admin(current_user)
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if usuario.tipo == "admin":
        raise HTTPException(status_code=400, detail="No puedes eliminar a otro administrador")
    _anonimizar_usuario(db, usuario_id, usuario)
    db.commit()
    return {"mensaje": "Usuario eliminado"}

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
            "telefono": f.telefono,
            "zona": f.zona,
            "email": usuario.email if usuario else None,
            "gremio": f.gremio or "fontanero",
            "verificado": f.verificado,
            "certificado_pro": f.certificado_pro,
            "disponible": f.disponible,
            "valoracion": f.valoracion,
            "num_trabajos": f.num_trabajos or 0,
            "stripe_conectado": bool(f.stripe_account_id),
            "comision_pendiente": round(comision_pendiente, 2),
            "bloqueado": usuario.bloqueado if usuario else False,
        })
    return resultado

@app.post("/admin/fontaneros/{fontanero_id}/comision-pendiente/marcar-pagada")
def admin_marcar_comision_pagada(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Para cobros en efectivo/Bizum/transferencia que un admin ha comprobado a
    mano (por ejemplo, tras ver que llegó la transferencia o el Bizum del
    profesional): liquida toda su comisión pendiente de golpe."""
    _verificar_admin(current_user)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    filas = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.comision_liquidada == False,
        models.Servicio.comision_aplicada != None,
    ).update({"comision_liquidada": True}, synchronize_session=False)
    db.commit()
    return {"mensaje": f"Comisión marcada como pagada en {filas} servicio(s)"}

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

@app.put("/admin/fontaneros/{fontanero_id}/certificar-pro")
def admin_certificar_pro(
    fontanero_id: int,
    certificar: bool = True,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Certificación propia 'Provenza Pro': un nivel por encima de la verificación
    básica, que el equipo concede tras evaluar personalmente al profesional."""
    _verificar_admin(current_user)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.certificado_pro = certificar
    if certificar and fontanero.usuario_id:
        _crear_notificacion(db, fontanero.usuario_id, "🏅 ¡Ya eres Provenza Pro!",
                             "Tu perfil ha sido certificado como profesional Provenza Pro", "certificacion_pro", fontanero_id)
    db.commit()
    return {"mensaje": "Certificado Provenza Pro actualizado" if certificar else "Certificación Provenza Pro retirada"}

@app.put("/admin/fontaneros/{fontanero_id}/editar")
def admin_editar_fontanero(
    fontanero_id: int,
    datos: schemas.AdminFontaneroEditar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Edición manual de un profesional desde el panel de administración
    (valoración, nombre, teléfono, zona, trabajos, disponibilidad)."""
    _verificar_admin(current_user)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    for campo, valor in datos.model_dump(exclude_unset=True).items():
        setattr(fontanero, campo, valor)
    db.commit()
    return {"mensaje": "Profesional actualizado"}

@app.get("/admin/documentos")
def admin_listar_documentos(
    estado: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Documentos de verificación de identidad, con la foto de perfil del profesional
    al lado para que el admin pueda compararlas visualmente antes de aprobar."""
    _verificar_admin(current_user)
    q = db.query(models.DocumentoVerificacion)
    if estado:
        q = q.filter(models.DocumentoVerificacion.estado == estado)
    docs = q.order_by(models.DocumentoVerificacion.creado_en.desc()).all()
    resultado = []
    for d in docs:
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == d.fontanero_id).first()
        resultado.append({
            "id": d.id,
            "tipo": d.tipo,
            "url": d.url,
            "estado": d.estado,
            "creado_en": d.creado_en,
            "fontanero_id": d.fontanero_id,
            "fontanero_nombre": fontanero.nombre if fontanero else None,
            "fontanero_foto_url": fontanero.foto_url if fontanero else None,
        })
    return resultado

@app.get("/admin/alertas")
def admin_listar_alertas(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Alertas automáticas: profesionales con cancelaciones o reseñas bajas repetidas."""
    _verificar_admin(current_user)
    alertas = db.query(models.AlertaAdmin).order_by(models.AlertaAdmin.creado_en.desc()).limit(100).all()
    resultado = []
    for a in alertas:
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == a.fontanero_id).first() if a.fontanero_id else None
        resultado.append({
            "id": a.id,
            "tipo": a.tipo,
            "mensaje": a.mensaje,
            "creado_en": a.creado_en,
            "fontanero_id": a.fontanero_id,
            "fontanero_nombre": fontanero.nombre if fontanero else None,
        })
    return resultado

@app.get("/admin/fraude")
def admin_deteccion_fraude(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Panel de detección de fraude: pagos en efectivo/bizum repetidamente no liquidados,
    y pares cliente-profesional con muchos servicios y siempre 5 estrellas en poco tiempo."""
    _verificar_admin(current_user)
    señales = []

    no_liquidados = (
        db.query(models.Servicio.fontanero_id, func.count(models.Servicio.id).label("total"))
        .filter(models.Servicio.comision_liquidada == False, models.Servicio.fontanero_id != None)
        .group_by(models.Servicio.fontanero_id)
        .having(func.count(models.Servicio.id) >= 3)
        .all()
    )
    for fontanero_id, total in no_liquidados:
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
        señales.append({
            "tipo": "efectivo_no_liquidado",
            "fontanero_id": fontanero_id,
            "fontanero_nombre": fontanero.nombre if fontanero else None,
            "detalle": f"{total} pagos en efectivo/bizum sin liquidar la comisión",
        })

    desde = datetime.datetime.utcnow() - datetime.timedelta(days=60)
    pares = (
        db.query(models.Servicio.cliente_id, models.Servicio.fontanero_id, func.count(models.Servicio.id).label("total"))
        .filter(models.Servicio.fontanero_id != None, models.Servicio.creado_en >= desde)
        .group_by(models.Servicio.cliente_id, models.Servicio.fontanero_id)
        .having(func.count(models.Servicio.id) >= 4)
        .all()
    )
    for cliente_id, fontanero_id, total in pares:
        resenas_par = db.query(models.Resena).join(
            models.Servicio, models.Resena.servicio_id == models.Servicio.id
        ).filter(
            models.Servicio.cliente_id == cliente_id,
            models.Servicio.fontanero_id == fontanero_id,
        ).all()
        if len(resenas_par) < 3:
            continue
        todas_perfectas = all((r.puntualidad + r.calidad + r.precio_justo + r.trato) / 4 >= 5 for r in resenas_par)
        if not todas_perfectas:
            continue
        cliente = db.query(models.Usuario).filter(models.Usuario.id == cliente_id).first()
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
        señales.append({
            "tipo": "resenas_cruzadas_sospechosas",
            "fontanero_id": fontanero_id,
            "fontanero_nombre": fontanero.nombre if fontanero else None,
            "detalle": f"{total} servicios en 60 días con {cliente.nombre if cliente else 'un mismo cliente'}, siempre 5 estrellas",
        })

    return señales

@app.get("/admin/servicios/{servicio_id}/auditoria")
def admin_auditoria_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    """Historial de cambios de precio y estado de un servicio."""
    _verificar_admin(current_user)
    return db.query(models.AuditoriaServicio).filter(
        models.AuditoriaServicio.servicio_id == servicio_id
    ).order_by(models.AuditoriaServicio.creado_en.asc()).all()

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

def _verificar_participante_servicio(db: Session, servicio_id: int, current_user: dict) -> models.Servicio:
    """Solo el cliente o el profesional asignado a un servicio pueden leer o
    escribir en su chat (evita leer/inyectar mensajes de conversaciones ajenas)."""
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    es_cliente = servicio.cliente_id == current_user["id"]
    fontanero_actual = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    es_fontanero = bool(fontanero_actual) and servicio.fontanero_id == fontanero_actual.id
    if not es_cliente and not es_fontanero:
        raise HTTPException(status_code=403, detail="No participas en este servicio")
    return servicio

class MensajeCrear(BaseModel):
    contenido: str

@app.post("/servicios/{servicio_id}/mensajes")
def enviar_mensaje(
    servicio_id: int,
    mensaje: MensajeCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = _verificar_participante_servicio(db, servicio_id, current_user)
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
        "imagen_url": nuevo.imagen_url,
        "remitente_tipo": emisor.tipo if emisor else "cliente",
        "remitente_nombre": emisor.nombre if emisor else "Usuario",
        "creado_en": str(nuevo.creado_en),
    }

@app.post("/servicios/{servicio_id}/mensajes/imagen")
def enviar_mensaje_imagen(
    servicio_id: int,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = _verificar_participante_servicio(db, servicio_id, current_user)
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    emisor_id = current_user["id"]
    emisor = db.query(models.Usuario).filter(models.Usuario.id == emisor_id).first()
    ext = _extension_segura(archivo.content_type, "jpg")
    nombre_archivo = f"{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(_leer_archivo_limitado(archivo))
    nuevo = models.Mensaje(
        servicio_id=servicio_id,
        emisor_id=emisor_id,
        texto="",
        imagen_url=f"/uploads/{nombre_archivo}",
        leido=False,
    )
    db.add(nuevo)
    if emisor_id == servicio.cliente_id and servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Nuevo mensaje", "📷 Foto", "mensaje", servicio_id)
    elif servicio.cliente_id != emisor_id:
        _crear_notificacion(db, servicio.cliente_id, "Nuevo mensaje", "📷 Foto", "mensaje", servicio_id)
    db.commit()
    db.refresh(nuevo)
    return {
        "id": nuevo.id,
        "contenido": "",
        "imagen_url": nuevo.imagen_url,
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
    _verificar_participante_servicio(db, servicio_id, current_user)
    mensajes = db.query(models.Mensaje).filter(
        models.Mensaje.servicio_id == servicio_id
    ).order_by(models.Mensaje.creado_en).all()
    resultado = []
    for m in mensajes:
        emisor = db.query(models.Usuario).filter(models.Usuario.id == m.emisor_id).first()
        resultado.append({
            "id": m.id,
            "contenido": m.texto,
            "imagen_url": m.imagen_url,
            "remitente_tipo": emisor.tipo if emisor else "cliente",
            "remitente_nombre": emisor.nombre if emisor else "Usuario",
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

# ─── SERVICIOS RECURRENTES: CREACIÓN AUTOMÁTICA ────────────────────────────────
# Hilo en segundo plano: cada 5 minutos revisa las plantillas activas y, cuando
# toca, crea el Servicio real (misma ruta que una solicitud normal) y avanza la
# fecha de la próxima ejecución según la frecuencia elegida.

def _bucle_servicios_recurrentes():
    from .database import SessionLocal
    while True:
        db = None
        try:
            db = SessionLocal()
            ahora = datetime.datetime.utcnow()
            pendientes = db.query(models.ServicioRecurrente).filter(
                models.ServicioRecurrente.activo == True,
                models.ServicioRecurrente.proxima_ejecucion <= ahora,
            ).all()
            for plantilla in pendientes:
                _crear_servicio_interno(
                    db, plantilla.cliente_id, plantilla.tipo, plantilla.descripcion,
                    False, None, plantilla.fontanero_id, plantilla.gremio,
                )
                dias = FRECUENCIA_A_DIAS.get(plantilla.frecuencia, 30)
                plantilla.proxima_ejecucion = plantilla.proxima_ejecucion + datetime.timedelta(days=dias)
            db.commit()
        except Exception as e:
            print(f"[servicios-recurrentes] error: {e}")
        finally:
            if db is not None:
                db.close()
        _time.sleep(300)

if os.getenv("DESACTIVAR_RECORDATORIOS", "") != "1":
    threading.Thread(target=_bucle_servicios_recurrentes, daemon=True).start()

# ─── URGENCIAS: AMPLIAR RADIO SI NADIE RESPONDE ────────────────────────────────
# Una urgencia sin fontanero directo se avisa primero solo a los profesionales
# de la misma ciudad. Si tras MINUTOS_ANTES_DE_AMPLIAR nadie la ha aceptado,
# se avisa también al resto del gremio en otras ciudades.

MINUTOS_ANTES_DE_AMPLIAR = 8

def _bucle_ampliar_radio_urgencias():
    from .database import SessionLocal
    while True:
        db = None
        try:
            db = SessionLocal()
            limite = datetime.datetime.utcnow() - datetime.timedelta(minutes=MINUTOS_ANTES_DE_AMPLIAR)
            pendientes = db.query(models.Servicio).filter(
                models.Servicio.urgente == True,
                models.Servicio.estado == "pendiente",
                models.Servicio.fontanero_id == None,
                models.Servicio.radio_ampliado == False,
                models.Servicio.ciudad != None,
                models.Servicio.creado_en <= limite,
            ).all()
            for servicio in pendientes:
                _notificar_urgencia(db, servicio, servicio.tipo, solo_ciudad=False)
                servicio.radio_ampliado = True
            db.commit()
        except Exception as e:
            print(f"[ampliar-radio-urgencias] error: {e}")
        finally:
            if db is not None:
                db.close()
        _time.sleep(120)

if os.getenv("DESACTIVAR_RECORDATORIOS", "") != "1":
    threading.Thread(target=_bucle_ampliar_radio_urgencias, daemon=True).start()
