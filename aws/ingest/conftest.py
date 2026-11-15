import os
import sys

# Raiz del servicio (api/, consumidores/, lib/) + fplib de la layer bajo ../shared/python.
# Se agrega al final: python/ tambien contiene ruedas aarch64 (salida de build.sh) que no deben tapar
# las dependencias del venv local.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(HERE)
sys.path.append(os.path.join(HERE, "..", "shared", "python"))
