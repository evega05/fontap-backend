---
name: model-column-migration
description: Recuerda y aplica el paso de agregar una columna nueva de un modelo SQLAlchemy a _migrar_columnas_faltantes en app/main.py, para que bases de datos ya existentes (producción) reciban la columna nueva al redeployar. Usar específicamente cuando se agrega una columna a un modelo existente en app/models.py. NO usar para crear un endpoint nuevo completo (usa backend-endpoint-scaffold) — esta skill es solo sobre el paso de migración de esquema.
---

# Alta de columna de modelo con migración

`create_all()` de SQLAlchemy (usado al arrancar la app) crea tablas nuevas
pero **no modifica tablas que ya existen** — si agregás una columna a un
modelo y el proyecto ya tiene una base de datos en producción, esa columna
nunca aparece ahí sin un paso extra. Este proyecto resuelve esto con un
diccionario a mano en `app/main.py`, función `_migrar_columnas_faltantes()`,
que se ejecuta en cada arranque y agrega con `ALTER TABLE` cualquier
columna que falte.

## Los dos pasos, siempre juntos

1. **`app/models.py`**: agregar la columna al modelo SQLAlchemy normal.
```python
nueva_columna = Column(String, nullable=True)
```

2. **`app/main.py`**, dentro de `_migrar_columnas_faltantes()`, en el
   diccionario `por_tabla`: agregar la entrada correspondiente a la tabla
   del modelo que tocaste, con su tipo SQL.
```python
"nombre_tabla": {
    ...columnas existentes...,
    "nueva_columna": "VARCHAR",  # o "FLOAT", "BOOLEAN DEFAULT FALSE", "INTEGER", "TIMESTAMP"
},
```

El nombre de la clave del diccionario debe ser el nombre de la **tabla**
(`__tablename__` del modelo, ej. `"fontaneros"`, `"servicios"`), no el
nombre de la clase Python. El nombre de la columna dentro debe coincidir
exacto con el atributo de SQLAlchemy.

## Tipos SQL usados en este proyecto (para consistencia)

- Texto: `"VARCHAR"` (corto) o `"TEXT"` (largo)
- Número decimal: `"FLOAT"`
- Entero: `"INTEGER"`
- Booleano: `"BOOLEAN DEFAULT FALSE"` o `"BOOLEAN DEFAULT TRUE"` (siempre con
  default explícito, para que filas ya existentes no queden con NULL en un
  campo booleano)
- Fecha/hora: `"TIMESTAMP"`

## Verificación

Si tenés un backend local corriendo contra una base de datos sqlite ya
creada previamente (no una nueva desde cero), reiniciá el proceso y
confirmá en los logs que no tira error, y opcionalmente:

```bash
sqlite3 /ruta/a/tu.db ".schema nombre_tabla" | grep nueva_columna
```

## Qué NO hacer

No uses una librería de migraciones tipo Alembic para esto — el proyecto
usa este mecanismo simple a propósito, no lo cambies sin que te lo pidan
explícitamente. Y no olvides el paso 2 aunque estés probando solo en local
con una base de datos nueva (`create_all()` sí la cubre ahí) — si no lo
agregás, producción se rompe en el próximo deploy.
