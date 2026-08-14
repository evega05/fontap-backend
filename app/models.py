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
    certificado_pro = Column(Boolean, default=False)  # certificación propia "Provenza Pro", evaluada por el equipo
    num_trabajos = Column(Integer, default=0)
    stripe_account_id = Column(String, nullable=True)
    comision_checkout_session = Column(String, nullable=True)
    codigo_referido = Column(String, nullable=True, unique=True)  # para el programa "trae a tu gremio"
    referido_por_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)
    referido_hasta = Column(DateTime, nullable=True)  # fin del periodo de comisión reducida para el que invitó
    primeros_trabajos_gratis = Column(Integer, default=3)  # cuántos leads gratis (sin comisión) le quedan por estrenar
    google_calendar_refresh_token = Column(String, nullable=True)
    google_calendar_conectado = Column(Boolean, default=False)
    nombre_empresa = Column(String, nullable=True)  # si tiene equipo, el nombre comercial que ven sus empleados
    empresa_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)  # si es empleado de otro profesional
    logo_empresa_url = Column(String, nullable=True)
    comision_empresa_porcentaje = Column(Float, nullable=True)  # % que se queda el dueño de lo que factura cada empleado
    aviso_automatico_activo = Column(Boolean, default=False)  # responde solo cuando está fuera de horario (disponible=False)

class Servicio(Base):
    __tablename__ = "servicios"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)
    catalogo_servicio_id = Column(Integer, ForeignKey("servicios_fontanero.id"), nullable=True)  # qué ítem del catálogo propio del profesional se pidió, si alguno
    tipo = Column(String)
    descripcion = Column(Text, nullable=True)
    urgente = Column(Boolean, default=False)
    urgencia_ia = Column(String, nullable=True)  # baja, media, alta, critica
    gremio = Column(String, nullable=True)  # ver GREMIOS_VALIDOS en main.py
    ciudad = Column(String, nullable=True)
    radio_ampliado = Column(Boolean, default=False)  # si ya se avisó a fontaneros fuera de la ciudad del cliente
    latitud_cliente = Column(Float, nullable=True)
    longitud_cliente = Column(Float, nullable=True)
    aviso_proximidad_enviado = Column(Boolean, default=False)
    es_consulta = Column(Boolean, default=False)  # True: solo quiere hablar antes de contratar, no se le fuerza precio/aceptar
    estado = Column(String, default="pendiente")
    precio = Column(Float, nullable=True)
    metodo_pago = Column(String, nullable=True)
    fecha = Column(DateTime, nullable=True)
    eta_minutos = Column(Integer, nullable=True)
    comision_aplicada = Column(Float, nullable=True)
    comision_liquidada = Column(Boolean, default=True)
    stripe_payment_intent = Column(String, nullable=True)
    google_event_id = Column(String, nullable=True)
    fontanero_preferente_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)
    prioridad_hasta = Column(DateTime, nullable=True)
    empresa_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)  # dueño al que le corresponde la comisión de este trabajo
    comision_empresa_aplicada = Column(Float, nullable=True)
    comision_empresa_liquidada = Column(Boolean, default=False)
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

class TareaEmpleado(Base):
    """Instrucción/aviso rápido que el dueño de la empresa le manda a un empleado —
    distinta de asignarle un Servicio (trabajo real de un cliente): esto es para avisos
    internos ("pasá a buscar materiales", "andá a la obra de la calle X"), opcionalmente
    ligados a un servicio si corresponde. La ubicación del empleado en "en_camino" se lee
    de Fontanero.latitud/longitud (el mismo tracking que ya existe), no se duplica acá."""
    __tablename__ = "tareas_empleado"
    id = Column(Integer, primary_key=True, index=True)
    empresa_id = Column(Integer, ForeignKey("fontaneros.id"))  # Fontanero.id del jefe
    empleado_id = Column(Integer, ForeignKey("fontaneros.id"))  # Fontanero.id del empleado
    servicio_id = Column(Integer, ForeignKey("servicios.id"), nullable=True)
    descripcion = Column(Text)
    estado = Column(String, default="pendiente")  # pendiente, aceptada, en_camino, terminada
    creado_en = Column(DateTime, default=utcnow)

class Mensaje(Base):
    __tablename__ = "mensajes"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"))
    emisor_id = Column(Integer, ForeignKey("usuarios.id"))
    texto = Column(Text)
    imagen_url = Column(String, nullable=True)
    leido = Column(Boolean, default=False)
    automatico = Column(Boolean, default=False)  # respuesta generada por el aviso de "fuera de horario"
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
    preferente = Column(Boolean, default=False)
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

class EstadoOAuth(Base):
    """Token de 'state' de un flujo OAuth (ej. Google Calendar) ligado al usuario
    que lo inició, para que el callback nunca tenga que confiar en un usuario_id
    que llega directo por query string (protección CSRF del flujo OAuth)."""
    __tablename__ = "estados_oauth"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    token = Column(String, index=True, unique=True)
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

class Proyecto(Base):
    __tablename__ = "proyectos"
    id = Column(Integer, primary_key=True, index=True)
    administrador_id = Column(Integer, ForeignKey("usuarios.id"))
    titulo = Column(String)
    descripcion = Column(Text, nullable=True)
    gremios = Column(String)  # coma-separada, ej. "electricista,fontanero,pintor"
    ciudad = Column(String, default="Bilbao")
    estado = Column(String, default="abierto")  # abierto, cerrado
    creado_en = Column(DateTime, default=utcnow)

class ProyectoInteres(Base):
    __tablename__ = "proyectos_interes"
    id = Column(Integer, primary_key=True, index=True)
    proyecto_id = Column(Integer, ForeignKey("proyectos.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    mensaje = Column(Text, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class OfertaEmpleo(Base):
    __tablename__ = "ofertas_empleo"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    gremio = Column(String)
    titulo = Column(String)
    descripcion = Column(Text, nullable=True)
    zona = Column(String, nullable=True)
    tipo_pago = Column(String, default="servicio")  # servicio (precio cerrado) u hora (tarifa/h)
    tarifa = Column(Float, nullable=True)
    activa = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=utcnow)

class OfertaEmpleoPostulante(Base):
    __tablename__ = "ofertas_empleo_postulantes"
    id = Column(Integer, primary_key=True, index=True)
    oferta_id = Column(Integer, ForeignKey("ofertas_empleo.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    mensaje = Column(Text, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class DocumentoVerificacion(Base):
    __tablename__ = "documentos_verificacion"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    tipo = Column(String)  # dni, carnet_profesional, seguro
    url = Column(String)
    estado = Column(String, default="pendiente")  # pendiente, verificado, rechazado
    creado_en = Column(DateTime, default=utcnow)

class AuditoriaServicio(Base):
    __tablename__ = "auditoria_servicios"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"))
    campo = Column(String)  # precio, estado
    valor_anterior = Column(String, nullable=True)
    valor_nuevo = Column(String, nullable=True)
    cambiado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    cambiado_por_tipo = Column(String, nullable=True)  # cliente, fontanero, sistema
    creado_en = Column(DateTime, default=utcnow)

class AlertaAdmin(Base):
    __tablename__ = "alertas_admin"
    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(String)  # cancelaciones_repetidas, resenas_bajas
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)
    mensaje = Column(Text)
    creado_en = Column(DateTime, default=utcnow)

class ListaNegraCliente(Base):
    __tablename__ = "lista_negra_cliente"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    creado_en = Column(DateTime, default=utcnow)

# ─── PANEL DE GESTIÓN (libro de obra del profesional) ──────────────────────────
# Herramientas internas de gestión del negocio de un profesional: agenda propia,
# ficha de clientes/leads, obras con checklist, presupuestos y nómina de un
# equipo que no tiene por qué usar la app (ayudantes, aprendices...). Todo
# pertenece a un fontanero_id (el dueño del negocio que lo gestiona).

class FichaCliente(Base):
    __tablename__ = "gestion_clientes"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    nombre = Column(String)
    telefono = Column(String, nullable=True)
    direccion = Column(String, nullable=True)
    frecuencia = Column(Float, nullable=True)  # visitas estimadas por semana
    notas = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)  # si viene de una contratación real (no dado de alta a mano)
    servicio_id = Column(Integer, ForeignKey("servicios.id"), nullable=True)  # último servicio real, para abrir el chat
    creado_en = Column(DateTime, default=utcnow)

class GestionLead(Base):
    __tablename__ = "gestion_leads"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    nombre = Column(String)
    telefono = Column(String, nullable=True)
    gremio = Column(String, nullable=True)
    mensaje = Column(Text, nullable=True)
    estado = Column(String, default="nuevo")  # nuevo, contactado, convertido, descartado
    creado_en = Column(DateTime, default=utcnow)

class GestionVisita(Base):
    __tablename__ = "gestion_visitas"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    cliente_id = Column(Integer, ForeignKey("gestion_clientes.id"))
    fecha = Column(String)  # YYYY-MM-DD
    hora = Column(String, nullable=True)
    tipo = Column(String, nullable=True)
    direccion = Column(String, nullable=True)
    notas = Column(Text, nullable=True)
    estado = Column(String, default="pendiente")  # pendiente, realizado

class GestionCobro(Base):
    __tablename__ = "gestion_cobros"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    cliente_id = Column(Integer, ForeignKey("gestion_clientes.id"))
    fecha = Column(String)
    hora = Column(String, nullable=True)
    importe = Column(Float)
    metodo = Column(String, default="Efectivo")
    estado = Column(String, default="pendiente")  # pendiente, cobrado

class GestionTarea(Base):
    __tablename__ = "gestion_tareas"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    descripcion = Column(String)
    fecha = Column(String)
    completada = Column(Boolean, default=False)

class GestionObra(Base):
    __tablename__ = "gestion_obras"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    nombre = Column(String)
    cliente_nombre = Column(String, nullable=True)
    direccion = Column(String, nullable=True)
    estado = Column(String, default="En curso")  # En curso, Pausada, Terminada
    fecha_inicio = Column(String, nullable=True)
    notas = Column(Text, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class GestionObraItem(Base):
    __tablename__ = "gestion_obra_items"
    id = Column(Integer, primary_key=True, index=True)
    obra_id = Column(Integer, ForeignKey("gestion_obras.id"))
    descripcion = Column(String)
    gremio = Column(String, nullable=True)
    completado = Column(Boolean, default=False)

class GestionObraAsignacion(Base):
    __tablename__ = "gestion_obra_asignaciones"
    id = Column(Integer, primary_key=True, index=True)
    obra_id = Column(Integer, ForeignKey("gestion_obras.id"))
    empleado_id = Column(Integer, ForeignKey("gestion_empleados.id"))
    fecha = Column(String, nullable=True)
    notas = Column(String, nullable=True)

class GestionPresupuesto(Base):
    __tablename__ = "gestion_presupuestos"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    cliente_nombre = Column(String)
    nombre = Column(String)
    estado = Column(String, default="Borrador")  # Borrador, Enviado, Aceptado, Rechazado
    fecha = Column(String)
    notas = Column(Text, nullable=True)
    iva = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class GestionPresupuestoLinea(Base):
    __tablename__ = "gestion_presupuesto_lineas"
    id = Column(Integer, primary_key=True, index=True)
    presupuesto_id = Column(Integer, ForeignKey("gestion_presupuestos.id"))
    concepto = Column(String)
    gremio = Column(String, nullable=True)
    cantidad = Column(Float, default=1)
    unidad = Column(String, default="ud")
    precio_unitario = Column(Float)

class GestionEmpleado(Base):
    __tablename__ = "gestion_empleados"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    nombre = Column(String)
    telefono = Column(String, nullable=True)
    tipo_pago = Column(String, default="hora")  # hora, dia, fijo
    tarifa = Column(Float, default=0)
    creado_en = Column(DateTime, default=utcnow)

class GestionJornada(Base):
    __tablename__ = "gestion_jornadas"
    id = Column(Integer, primary_key=True, index=True)
    empleado_id = Column(Integer, ForeignKey("gestion_empleados.id"))
    fecha = Column(String)
    pagado = Column(Boolean, default=False)

class GestionPagoEmpleado(Base):
    __tablename__ = "gestion_pagos_empleado"
    id = Column(Integer, primary_key=True, index=True)
    empleado_id = Column(Integer, ForeignKey("gestion_empleados.id"))
    fecha = Column(String)
    importe = Column(Float)
    concepto = Column(String, nullable=True)
