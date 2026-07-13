from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from .database import Base
import datetime

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    telefono = Column(String)
    password_hash = Column(String, nullable=False)
    tipo = Column(String, default="cliente")  # cliente, fontanero, admin, administrador_fincas
    terminos_aceptados = Column(Boolean, default=False)
    email_verificado = Column(Boolean, default=False)
    telefono_verificado = Column(Boolean, default=False)
    bloqueado = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class Fontanero(Base):
    __tablename__ = "fontaneros"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    nombre = Column(String)
    telefono = Column(String)
    zona = Column(String, default="Bilbao")
    disponible = Column(Boolean, default=True)
    disponible_24h = Column(Boolean, default=False)
    valoracion = Column(Float, nullable=True)
    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)
    ubicacion_actualizada = Column(DateTime, nullable=True)
    foto_url = Column(String, nullable=True)
    descripcion = Column(Text, nullable=True)
    especialidades = Column(Text, nullable=True)
    vacaciones_desde = Column(DateTime, nullable=True)
    vacaciones_hasta = Column(DateTime, nullable=True)
    gremio = Column(String, default="fontanero")  # ver GREMIOS_VALIDOS en main.py
    verificado = Column(Boolean, default=False)
    num_trabajos = Column(Integer, default=0)
    stripe_account_id = Column(String, nullable=True)
    comision_checkout_session = Column(String, nullable=True)

class Servicio(Base):
    __tablename__ = "servicios"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)
    tipo = Column(String)
    descripcion = Column(Text, nullable=True)
    urgente = Column(Boolean, default=False)
    urgencia_ia = Column(String, nullable=True)  # baja, media, alta, critica
    gremio = Column(String, nullable=True)  # ver GREMIOS_VALIDOS en main.py
    estado = Column(String, default="pendiente")
    precio = Column(Float, nullable=True)
    metodo_pago = Column(String, nullable=True)
    fecha = Column(DateTime, nullable=True)
    eta_minutos = Column(Integer, nullable=True)
    comision_aplicada = Column(Float, nullable=True)
    comision_liquidada = Column(Boolean, default=True)
    stripe_payment_intent = Column(String, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class ServicioFontanero(Base):
    __tablename__ = "servicios_fontanero"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    nombre = Column(String)
    precio = Column(Float)
    duracion_minutos = Column(Integer, default=60)
    activo = Column(Boolean, default=True)

class HorarioBase(Base):
    __tablename__ = "horarios_base"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    dia_semana = Column(Integer)
    hora_inicio = Column(String)
    hora_fin = Column(String)
    intervalo_minutos = Column(Integer, default=60)

class BloqueoHorario(Base):
    __tablename__ = "bloqueos_horario"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    fecha = Column(DateTime)
    hora_inicio = Column(String)
    hora_fin = Column(String)
    motivo = Column(String, nullable=True)

class GaleriaFontanero(Base):
    __tablename__ = "galeria_fontanero"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    url = Column(String)
    descripcion = Column(String, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class ImagenServicio(Base):
    __tablename__ = "imagenes_servicio"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"))
    url = Column(String)
    creado_en = Column(DateTime, default=utcnow)

class Mensaje(Base):
    __tablename__ = "mensajes"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"))
    emisor_id = Column(Integer, ForeignKey("usuarios.id"))
    texto = Column(Text)
    imagen_url = Column(String, nullable=True)
    leido = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class TokenPush(Base):
    __tablename__ = "tokens_push"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    token = Column(String, unique=True)
    plataforma = Column(String, default="android")
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=utcnow)

class Notificacion(Base):
    __tablename__ = "notificaciones"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    titulo = Column(String)
    cuerpo = Column(String)
    tipo = Column(String, nullable=True)
    referencia_id = Column(Integer, nullable=True)
    leida = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class Favorito(Base):
    __tablename__ = "favoritos"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    creado_en = Column(DateTime, default=utcnow)

class Oferta(Base):
    __tablename__ = "ofertas"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    precio = Column(Float)
    materiales = Column(Float, nullable=True)
    mano_obra = Column(Float, nullable=True)
    mensaje = Column(Text, nullable=True)
    estado = Column(String, default="pendiente")  # pendiente, aceptada, rechazada
    creado_en = Column(DateTime, default=utcnow)

class ListaEspera(Base):
    __tablename__ = "lista_espera"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    gremio = Column(String)
    creado_en = Column(DateTime, default=utcnow)

class Resena(Base):
    __tablename__ = "resenas"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"), unique=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    puntualidad = Column(Float)
    calidad = Column(Float)
    precio_justo = Column(Float)
    trato = Column(Float)
    comentario = Column(Text, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class ResenaCliente(Base):
    __tablename__ = "resenas_cliente"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"), unique=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    puntualidad = Column(Float)
    trato = Column(Float)
    comunicacion = Column(Float)
    comentario = Column(Text, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class VerificacionSMS(Base):
    __tablename__ = "verificaciones_sms"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    codigo = Column(String, index=True)
    telefono = Column(String)
    expira = Column(DateTime)
    usado = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class VerificacionEmail(Base):
    __tablename__ = "verificaciones_email"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    token = Column(String, index=True)
    expira = Column(DateTime)
    usado = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class PasswordReset(Base):
    __tablename__ = "password_resets"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    token = Column(String, index=True)
    expira = Column(DateTime)
    usado = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class Cita(Base):
    __tablename__ = "citas"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    servicio_id = Column(Integer, ForeignKey("servicios.id"), nullable=True)
    titulo = Column(String)
    fecha_inicio = Column(DateTime)
    fecha_fin = Column(DateTime)
    recordatorio_24h = Column(Boolean, default=False)
    recordatorio_1h = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class Inmueble(Base):
    __tablename__ = "inmuebles"
    id = Column(Integer, primary_key=True, index=True)
    administrador_id = Column(Integer, ForeignKey("usuarios.id"))
    nombre = Column(String)
    direccion = Column(String)
    ciudad = Column(String, default="Bilbao")
    creado_en = Column(DateTime, default=utcnow)

class ServicioRecurrente(Base):
    __tablename__ = "servicios_recurrentes"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)
    gremio = Column(String)
    tipo = Column(String)
    descripcion = Column(Text, nullable=True)
    frecuencia = Column(String)  # semanal, quincenal, mensual
    proxima_ejecucion = Column(DateTime)
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=utcnow)

class DocumentoVerificacion(Base):
    __tablename__ = "documentos_verificacion"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    tipo = Column(String)  # dni, carnet_profesional, seguro
    url = Column(String)
    estado = Column(String, default="pendiente")  # pendiente, verificado, rechazado
    creado_en = Column(DateTime, default=utcnow)
