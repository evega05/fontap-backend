from pydantic import BaseModel, EmailStr
from typing import Optional, Literal
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
    tipo: Literal["cliente", "fontanero"] = "cliente"

class UsuarioLogin(BaseModel):
    email: EmailStr
    password: str

class UsuarioRespuesta(BaseModel):
    id: int
    nombre: str
    email: str
    telefono: str
    tipo: str
    class Config:
        from_attributes = True

class FontaneroRespuesta(BaseModel):
    id: int
    nombre: Optional[str] = None
    zona: Optional[str] = None
    disponible: bool
    disponible_24h: bool = False
    valoracion: Optional[float] = None
    foto_url: Optional[str] = None
    class Config:
        from_attributes = True

class ServicioCrear(BaseModel):
    tipo: str
    descripcion: Optional[str] = None
    urgente: bool = False
    fecha: Optional[datetime.datetime] = None

class ServicioRespuesta(BaseModel):
    id: int
    cliente_id: int
    fontanero_id: Optional[int] = None
    tipo: str
    descripcion: Optional[str] = None
    urgente: bool
    estado: str
    precio: Optional[float] = None
    fecha: Optional[datetime.datetime] = None
    class Config:
        from_attributes = True

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