import os
import secrets
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from . import models
from .database import get_db

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    # Sin SECRET_KEY en el entorno, cualquiera podría firmar JWTs válidos si usáramos
    # un valor por defecto fijo (el código es público). Generamos uno aleatorio por
    # proceso: es seguro, aunque invalida las sesiones existentes en cada reinicio.
    print("[auth] AVISO: falta la variable de entorno SECRET_KEY. Generando una "
          "clave aleatoria temporal (las sesiones no sobrevivirán un reinicio). "
          "Define SECRET_KEY en las variables de entorno de producción.")
    SECRET_KEY = secrets.token_hex(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def verificar_password(password, hash):
    return bcrypt.checkpw(password.encode("utf-8"), hash.encode("utf-8"))

def hashear_password(password):
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

def crear_token(data: dict):
    datos = data.copy()
    expira = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    datos.update({"exp": expira})
    return jwt.encode(datos, SECRET_KEY, algorithm=ALGORITHM)

def verificar_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if email is None:
            return None
        return payload
    except JWTError:
        return None

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    payload = verificar_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # El JWT en sí no se puede revocar, pero comprobamos el estado actual en BD en
    # cada request: así, si un admin veta o elimina la cuenta después de emitido
    # el token, deja de servir aunque todavía no haya caducado (hasta 7 días).
    usuario_id = payload.get("id")
    if usuario_id is not None:
        usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
        if usuario is None or usuario.bloqueado:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido o expirado",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return payload