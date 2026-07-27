from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Literal, List
import datetime

class Token(BaseModel):
    access_token: str
    token_type: str
    tipo_usuario: str
    nombre: Optional[str] = None
    id: Optional[int] = None
    email: Optional[str] = None

class UsuarioRegistro(BaseModel):
    nombre: str
    email: EmailStr
    telefono: str
    password: str
    tipo: Literal["cliente", "fontanero", "administrador_fincas"] = "cliente"
    terminos_aceptados: bool = False
    gremio: Literal[
        "fontanero", "electricista", "cerrajero", "pintor", "carpintero",
        "albanil", "climatizacion", "jardinero", "limpieza", "mudanzas",
        "montador", "cristalero",
    ] = "fontanero"
    codigo_referido: Optional[str] = None  # código de otro profesional del mismo gremio ("trae a tu gremio")

class UsuarioLogin(BaseModel):
    email: EmailStr
    password: str

class UsuarioRespuesta(BaseModel):
    id: int
    nombre: str
    email: str
    telefono: str
    tipo: str
    email_verificado: Optional[bool] = False
    bloqueado: Optional[bool] = False
    creado_en: Optional[datetime.datetime] = None
    class Config:
        from_attributes = True

class FontaneroRespuesta(BaseModel):
    id: int
    usuario_id: Optional[int] = None
    nombre: Optional[str] = None
    telefono: Optional[str] = None
    zona: Optional[str] = None
    disponible: bool
    disponible_24h: bool = False
    valoracion: Optional[float] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    ubicacion_actualizada: Optional[datetime.datetime] = None
    foto_url: Optional[str] = None
    descripcion: Optional[str] = None
    especialidades: Optional[str] = None
    vacaciones_desde: Optional[datetime.datetime] = None
    vacaciones_hasta: Optional[datetime.datetime] = None
    gremio: Optional[str] = None
    verificado: bool = False
    certificado_pro: bool = False
    num_trabajos: int = 0
    precio_desde: Optional[float] = None
    servicios: List[str] = []
    codigo_referido: Optional[str] = None
    primeros_trabajos_gratis: int = 0
    google_calendar_conectado: bool = False
    miembro_desde: Optional[datetime.datetime] = None
    favorito_preferente: bool = False
    nombre_empresa: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    logo_empresa_url: Optional[str] = None
    equipo_valoracion_media: Optional[float] = None
    equipo_num_trabajos: int = 0
    equipo_num_miembros: int = 0
    class Config:
        from_attributes = True

class FavoritoPreferenteActualizar(BaseModel):
    preferente: bool

class EmpresaActualizar(BaseModel):
    nombre_empresa: Optional[str] = None
    comision_empresa_porcentaje: Optional[float] = None

class ComisionEmpresaEmpleado(BaseModel):
    empleado_id: int
    empleado_nombre: str
    total: float
    num_servicios: int

class ComisionEmpresaRespuesta(BaseModel):
    total: float
    por_empleado: List[ComisionEmpresaEmpleado]

class ComisionEmpresaLiquidar(BaseModel):
    empleado_fontanero_id: int

class EquipoInvitarCrear(BaseModel):
    email: EmailStr

class EquipoAceptarCrear(BaseModel):
    empresa_fontanero_id: int

class EquipoMiembroRespuesta(BaseModel):
    id: int
    nombre: str
    telefono: Optional[str] = None
    zona: Optional[str] = None
    disponible: bool
    valoracion: Optional[float] = None
    num_trabajos: int = 0
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    ubicacion_actualizada: Optional[datetime.datetime] = None
    class Config:
        from_attributes = True

class ServicioAsignarEmpleado(BaseModel):
    empleado_fontanero_id: int

class FontaneroActualizar(BaseModel):
    zona: Optional[str] = None
    descripcion: Optional[str] = None
    especialidades: Optional[str] = None
    disponible_24h: Optional[bool] = None
    gremio: Optional[str] = None

class AdminFontaneroEditar(BaseModel):
    nombre: Optional[str] = None
    telefono: Optional[str] = None
    zona: Optional[str] = None
    valoracion: Optional[float] = None
    num_trabajos: Optional[int] = None
    disponible: Optional[bool] = None
    verificado: Optional[bool] = None

class UbicacionActualizar(BaseModel):
    latitud: float
    longitud: float

class VacacionesCrear(BaseModel):
    desde: datetime.datetime
    hasta: datetime.datetime

class GaleriaCrear(BaseModel):
    descripcion: Optional[str] = None

class GaleriaRespuesta(BaseModel):
    id: int
    fontanero_id: int
    url: str
    descripcion: Optional[str] = None
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class EstadisticasRespuesta(BaseModel):
    trabajos_completados: int
    ingresos_totales: float
    valoracion_media: Optional[float] = None
    tasa_aceptacion: float

class ServicioReprogramar(BaseModel):
    fecha: datetime.datetime

class ServicioCrear(BaseModel):
    tipo: str
    descripcion: Optional[str] = None
    urgente: bool = False
    fecha: Optional[datetime.datetime] = None
    fontanero_id: Optional[int] = None
    gremio: Optional[str] = None
    ciudad: Optional[str] = None
    latitud_cliente: Optional[float] = None
    longitud_cliente: Optional[float] = None
    es_consulta: bool = False

ESTADO_COLORES = {
    "pendiente": "#D97706",
    "aceptado": "#0A7A3E",
    "precio_enviado": "#1A56DB",
    "precio_aceptado": "#1A56DB",
    "en_camino": "#1A56DB",
    "completado": "#0A7A3E",
    "pago_pendiente": "#7356BF",
    "pagado": "#0A7A3E",
    "rechazado": "#C8271A",
    "cancelado": "#C8271A",
}

class ServicioRespuesta(BaseModel):
    id: int
    cliente_id: int
    fontanero_id: Optional[int] = None
    tipo: str
    descripcion: Optional[str] = None
    urgente: bool
    urgencia_ia: Optional[str] = None
    es_consulta: bool = False
    estado: str
    estado_color: Optional[str] = None
    precio: Optional[float] = None
    fecha: Optional[datetime.datetime] = None
    eta_minutos: Optional[int] = None
    fontanero_nombre: Optional[str] = None
    num_ofertas: int = 0

    @classmethod
    def from_orm_with_color(cls, obj):
        data = cls.model_validate(obj)
        data.estado_color = ESTADO_COLORES.get(obj.estado, "#D97706")
        return data

    class Config:
        from_attributes = True

class ETAUpdate(BaseModel):
    eta_minutos: int

class ServicioFontaneroCrear(BaseModel):
    nombre: str
    precio: float
    duracion_minutos: int = 60

class ServicioFontaneroRespuesta(BaseModel):
    id: int
    fontanero_id: int
    nombre: str
    precio: float
    duracion_minutos: int
    activo: bool
    class Config:
        from_attributes = True

class HorarioBaseCrear(BaseModel):
    dia_semana: int
    hora_inicio: str
    hora_fin: str
    intervalo_minutos: int = 60

class HorarioBaseRespuesta(BaseModel):
    id: int
    fontanero_id: int
    dia_semana: int
    hora_inicio: str
    hora_fin: str
    intervalo_minutos: int
    class Config:
        from_attributes = True

class BloqueoCrear(BaseModel):
    fecha: datetime.datetime
    hora_inicio: str
    hora_fin: str
    motivo: Optional[str] = None

class BloqueoRespuesta(BaseModel):
    id: int
    fontanero_id: int
    fecha: datetime.datetime
    hora_inicio: str
    hora_fin: str
    motivo: Optional[str] = None
    class Config:
        from_attributes = True

class ImagenRespuesta(BaseModel):
    id: int
    servicio_id: int
    url: str
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class MensajeCrear(BaseModel):
    texto: str

class MensajeRespuesta(BaseModel):
    id: int
    servicio_id: int
    emisor_id: int
    texto: str
    imagen_url: Optional[str] = None
    leido: bool
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class TokenPushCrear(BaseModel):
    token: str
    plataforma: str = "android"

class NotificacionRespuesta(BaseModel):
    id: int
    usuario_id: int
    titulo: str
    cuerpo: str
    tipo: Optional[str] = None
    referencia_id: Optional[int] = None
    leida: bool
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class FavoritoRespuesta(BaseModel):
    id: int
    cliente_id: int
    fontanero_id: int
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class OfertaCrear(BaseModel):
    precio: Optional[float] = Field(default=None, ge=0)
    materiales: Optional[float] = Field(default=None, ge=0)
    mano_obra: Optional[float] = Field(default=None, ge=0)
    mensaje: Optional[str] = None

class OfertaRespuesta(BaseModel):
    id: int
    servicio_id: int
    fontanero_id: int
    precio: float
    materiales: Optional[float] = None
    mano_obra: Optional[float] = None
    mensaje: Optional[str] = None
    estado: str
    creado_en: datetime.datetime
    fontanero_nombre: Optional[str] = None
    fontanero_valoracion: Optional[float] = None
    fontanero_zona: Optional[str] = None
    fontanero_trabajos: Optional[int] = None
    tipo: Optional[str] = None
    zona: Optional[str] = None
    class Config:
        from_attributes = True

class ListaEsperaCrear(BaseModel):
    gremio: str

class ListaEsperaRespuesta(BaseModel):
    id: int
    cliente_id: int
    gremio: str
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class ServicioRecurrenteCrear(BaseModel):
    gremio: str
    tipo: str
    descripcion: Optional[str] = None
    frecuencia: Literal["semanal", "quincenal", "mensual"]
    fontanero_id: Optional[int] = None
    proxima_ejecucion: datetime.datetime

class ServicioRecurrenteActualizar(BaseModel):
    activo: Optional[bool] = None
    frecuencia: Optional[Literal["semanal", "quincenal", "mensual"]] = None

class ServicioRecurrenteRespuesta(BaseModel):
    id: int
    cliente_id: int
    fontanero_id: Optional[int] = None
    gremio: str
    tipo: str
    descripcion: Optional[str] = None
    frecuencia: str
    proxima_ejecucion: datetime.datetime
    activo: bool
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class ProyectoCrear(BaseModel):
    titulo: str
    descripcion: Optional[str] = None
    gremios: List[str]
    ciudad: str = "Bilbao"

class ProyectoRespuesta(BaseModel):
    id: int
    administrador_id: int
    titulo: str
    descripcion: Optional[str] = None
    gremios: str
    ciudad: str
    estado: str
    creado_en: datetime.datetime
    num_interesados: int = 0
    class Config:
        from_attributes = True

class ProyectoActualizar(BaseModel):
    estado: Optional[Literal["abierto", "cerrado"]] = None

class ProyectoInteresCrear(BaseModel):
    mensaje: Optional[str] = None

class ProyectoInteresRespuesta(BaseModel):
    id: int
    proyecto_id: int
    fontanero_id: int
    mensaje: Optional[str] = None
    creado_en: datetime.datetime
    fontanero_nombre: Optional[str] = None
    fontanero_valoracion: Optional[float] = None
    class Config:
        from_attributes = True

class ResenaCrear(BaseModel):
    puntualidad: float
    calidad: float
    precio_justo: float
    trato: float
    comentario: Optional[str] = None

class ResenaRespuesta(BaseModel):
    id: int
    servicio_id: int
    cliente_id: int
    fontanero_id: int
    puntualidad: float
    calidad: float
    precio_justo: float
    trato: float
    comentario: Optional[str] = None
    creado_en: datetime.datetime
    cliente_nombre: Optional[str] = None
    fontanero_nombre: Optional[str] = None

class ResenaClienteCrear(BaseModel):
    puntualidad: float
    trato: float
    comunicacion: float
    comentario: Optional[str] = None

class ResenaClienteRespuesta(BaseModel):
    id: int
    servicio_id: int
    fontanero_id: int
    cliente_id: int
    puntualidad: float
    trato: float
    comunicacion: float
    comentario: Optional[str] = None
    creado_en: datetime.datetime
    class Config:
        from_attributes = True
    class Config:
        from_attributes = True

class CitaCrear(BaseModel):
    titulo: str
    fecha_inicio: datetime.datetime
    fecha_fin: datetime.datetime
    servicio_id: Optional[int] = None

class CitaRespuesta(BaseModel):
    id: int
    fontanero_id: int
    servicio_id: Optional[int] = None
    titulo: str
    fecha_inicio: datetime.datetime
    fecha_fin: datetime.datetime
    class Config:
        from_attributes = True

class InmuebleCrear(BaseModel):
    nombre: str
    direccion: str
    ciudad: str = "Bilbao"

class InmuebleRespuesta(BaseModel):
    id: int
    administrador_id: int
    nombre: str
    direccion: str
    ciudad: str
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class OfertaEmpleoCrear(BaseModel):
    titulo: str
    descripcion: Optional[str] = None
    zona: Optional[str] = None

class OfertaEmpleoActualizar(BaseModel):
    activa: Optional[bool] = None

class OfertaEmpleoRespuesta(BaseModel):
    id: int
    fontanero_id: int
    gremio: str
    titulo: str
    descripcion: Optional[str] = None
    zona: Optional[str] = None
    activa: bool
    creado_en: datetime.datetime
    fontanero_nombre: Optional[str] = None
    fontanero_valoracion: Optional[float] = None
    num_postulantes: int = 0
    class Config:
        from_attributes = True

class OfertaEmpleoPostularCrear(BaseModel):
    mensaje: Optional[str] = None

class OfertaEmpleoPostulanteRespuesta(BaseModel):
    id: int
    oferta_id: int
    fontanero_id: int
    mensaje: Optional[str] = None
    creado_en: datetime.datetime
    fontanero_nombre: Optional[str] = None
    fontanero_telefono: Optional[str] = None
    fontanero_valoracion: Optional[float] = None
    class Config:
        from_attributes = True

class DocumentoCrear(BaseModel):
    tipo: str

class DocumentoRespuesta(BaseModel):
    id: int
    fontanero_id: int
    tipo: str
    url: str
    estado: str
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class GestionClienteCrear(BaseModel):
    nombre: str
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    frecuencia: Optional[float] = None
    notas: Optional[str] = None

class GestionClienteActualizar(BaseModel):
    nombre: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    frecuencia: Optional[float] = None
    notas: Optional[str] = None

class GestionClienteRespuesta(BaseModel):
    id: int
    fontanero_id: int
    nombre: str
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    frecuencia: Optional[float] = None
    notas: Optional[str] = None
    creado_en: datetime.datetime
    ultima_visita: Optional[str] = None
    class Config:
        from_attributes = True

class GestionLeadCrear(BaseModel):
    nombre: str
    telefono: Optional[str] = None
    gremio: Optional[str] = None
    mensaje: Optional[str] = None

class GestionLeadActualizar(BaseModel):
    estado: Literal["nuevo", "contactado", "convertido", "descartado"]

class GestionLeadRespuesta(BaseModel):
    id: int
    fontanero_id: int
    nombre: str
    telefono: Optional[str] = None
    gremio: Optional[str] = None
    mensaje: Optional[str] = None
    estado: str
    creado_en: datetime.datetime
    class Config:
        from_attributes = True

class GestionVisitaCrear(BaseModel):
    cliente_nombre: str
    fecha: str
    hora: Optional[str] = None
    tipo: Optional[str] = None
    direccion: Optional[str] = None
    notas: Optional[str] = None

class GestionVisitaRespuesta(BaseModel):
    id: int
    fontanero_id: int
    cliente_id: int
    fecha: str
    hora: Optional[str] = None
    tipo: Optional[str] = None
    direccion: Optional[str] = None
    notas: Optional[str] = None
    estado: str
    cliente_nombre: Optional[str] = None
    class Config:
        from_attributes = True

class GestionCobroCrear(BaseModel):
    cliente_nombre: str
    fecha: str
    hora: Optional[str] = None
    importe: float
    metodo: str = "Efectivo"

class GestionCobroRespuesta(BaseModel):
    id: int
    fontanero_id: int
    cliente_id: int
    fecha: str
    hora: Optional[str] = None
    importe: float
    metodo: str
    estado: str
    cliente_nombre: Optional[str] = None
    class Config:
        from_attributes = True

class GestionTareaCrear(BaseModel):
    descripcion: str
    fecha: str

class GestionTareaRespuesta(BaseModel):
    id: int
    fontanero_id: int
    descripcion: str
    fecha: str
    completada: bool
    class Config:
        from_attributes = True

class GestionObraCrear(BaseModel):
    nombre: str
    cliente_nombre: Optional[str] = None
    direccion: Optional[str] = None
    estado: str = "En curso"
    fecha_inicio: Optional[str] = None
    notas: Optional[str] = None

class GestionObraActualizar(BaseModel):
    nombre: Optional[str] = None
    cliente_nombre: Optional[str] = None
    direccion: Optional[str] = None
    estado: Optional[Literal["En curso", "Pausada", "Terminada"]] = None
    fecha_inicio: Optional[str] = None
    notas: Optional[str] = None

class GestionObraItemCrear(BaseModel):
    descripcion: str
    gremio: Optional[str] = None

class GestionObraItemRespuesta(BaseModel):
    id: int
    obra_id: int
    descripcion: str
    gremio: Optional[str] = None
    completado: bool
    class Config:
        from_attributes = True

class GestionObraAsignacionCrear(BaseModel):
    empleado_id: int
    fecha: Optional[str] = None
    notas: Optional[str] = None

class GestionObraAsignacionRespuesta(BaseModel):
    id: int
    obra_id: int
    empleado_id: int
    fecha: Optional[str] = None
    notas: Optional[str] = None
    empleado_nombre: Optional[str] = None
    empleado_telefono: Optional[str] = None
    class Config:
        from_attributes = True

class GestionObraRespuesta(BaseModel):
    id: int
    fontanero_id: int
    nombre: str
    cliente_nombre: Optional[str] = None
    direccion: Optional[str] = None
    estado: str
    fecha_inicio: Optional[str] = None
    notas: Optional[str] = None
    creado_en: datetime.datetime
    items: List[GestionObraItemRespuesta] = []
    asignaciones: List[GestionObraAsignacionRespuesta] = []
    class Config:
        from_attributes = True

class GestionPresupuestoLineaCrear(BaseModel):
    concepto: str
    gremio: Optional[str] = None
    cantidad: float = 1
    unidad: str = "ud"
    precio_unitario: float

class GestionPresupuestoLineaRespuesta(BaseModel):
    id: int
    presupuesto_id: int
    concepto: str
    gremio: Optional[str] = None
    cantidad: float
    unidad: str
    precio_unitario: float
    class Config:
        from_attributes = True

class GestionPresupuestoCrear(BaseModel):
    nombre: str
    cliente_nombre: str
    estado: str = "Borrador"
    fecha: str
    notas: Optional[str] = None
    iva: bool = False
    lineas: List[GestionPresupuestoLineaCrear] = []

class GestionPresupuestoActualizar(BaseModel):
    nombre: Optional[str] = None
    estado: Optional[Literal["Borrador", "Enviado", "Aceptado", "Rechazado"]] = None
    fecha: Optional[str] = None
    notas: Optional[str] = None
    iva: Optional[bool] = None

class GestionPresupuestoRespuesta(BaseModel):
    id: int
    fontanero_id: int
    cliente_nombre: str
    nombre: str
    estado: str
    fecha: str
    notas: Optional[str] = None
    iva: bool
    creado_en: datetime.datetime
    lineas: List[GestionPresupuestoLineaRespuesta] = []
    subtotal: float = 0
    total: float = 0
    class Config:
        from_attributes = True

class GestionEmpleadoCrear(BaseModel):
    nombre: str
    telefono: Optional[str] = None
    tipo_pago: Literal["hora", "dia", "fijo"] = "hora"
    tarifa: float = 0

class GestionEmpleadoRespuesta(BaseModel):
    id: int
    fontanero_id: int
    nombre: str
    telefono: Optional[str] = None
    tipo_pago: str
    tarifa: float
    creado_en: datetime.datetime
    dias_pendientes: int = 0
    pagado_este_mes: float = 0
    class Config:
        from_attributes = True

class GestionPagoEmpleadoCrear(BaseModel):
    fecha: str
    importe: float
    concepto: Optional[str] = None

class GestionPagoEmpleadoRespuesta(BaseModel):
    id: int
    empleado_id: int
    fecha: str
    importe: float
    concepto: Optional[str] = None
    class Config:
        from_attributes = True

class GestionJornadaRespuesta(BaseModel):
    id: int
    empleado_id: int
    fecha: str
    pagado: bool
    class Config:
        from_attributes = True

class AdminStats(BaseModel):
    total_usuarios: int
    total_fontaneros: int
    total_clientes: int
    total_servicios: int
    servicios_pendientes: int
    servicios_completados: int
    ingresos_plataforma: float
    dinero_movido: float = 0
    comisiones_pendientes: float = 0
    usuarios_nuevos_7d: int = 0
    servicios_7d: int = 0
    usuarios_bloqueados: int = 0
    por_gremio: Optional[dict] = None
