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
    telefono_verificado: Optional[bool] = False
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
    class Config:
        from_attributes = True

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

ESTADO_COLORES = {
    "pendiente": "#D97706",
    "aceptado": "#0A7A3E",
    "en_camino": "#1A56DB",
    "rechazado": "#C8271A",
    "pagado": "#0A7A3E",
    "precio_enviado": "#1A56DB",
    "completado": "#0A7A3E",
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
