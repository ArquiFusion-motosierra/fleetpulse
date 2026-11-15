import os
import sys

# Raiz del servicio (pub/) + fplib de la layer bajo ../../shared/python.
# Se agrega al final: python/ tambien contiene ruedas aarch64 que no deben tapar el venv local.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(HERE)
sys.path.append(os.path.join(HERE, "..", "..", "shared", "python"))
