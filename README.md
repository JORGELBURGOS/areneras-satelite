# Barrido satelital de canteras y frentes de arena

Detecta en imágenes Sentinel-2 los lugares donde hay **suelo desnudo, claro y persistente durante todo el año**
(la huella típica de una cantera o un acopio de arena) y marca si son **nuevos** o están **creciendo** respecto de dos años antes.

La Argentina se divide en cuadrantes de 0,25° (unos 27 × 27 km). Una cola en la base de datos indica cuáles mirar primero.
Cada 6 horas, dos trabajadores toman cuadrantes de a uno, los analizan y guardan los candidatos. Los cuadrantes prioritarios
se vuelven a mirar cada 6 meses y el resto una vez por año.

- **Datos:** Copernicus Sentinel-2 L2A, a través del catálogo abierto de Microsoft Planetary Computer.
- **Método:** ver la explicación al comienzo de `scripts/territorio/sites_satellite.py`.
- **Este repositorio es público y no contiene datos de empresas ni de clientes.** Solo el código del análisis de imágenes.
  Los resultados se guardan en una base privada mediante un usuario que solo puede leer la cola de cuadrantes y escribir detecciones.

## Configuración

Un único secreto en *Settings → Secrets and variables → Actions*: `SATELITE_DATABASE_URL`.

## Correr a mano

*Actions → Barrido satelital → Run workflow*. Para probar, poné `1` en "Máximo de cuadrantes".
