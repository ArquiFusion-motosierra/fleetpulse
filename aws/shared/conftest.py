import os
import sys

# Mismo layout que la layer: fplib/ bajo python/.
# Se agrega al final: python/ tambien contiene ruedas aarch64 (salida de build.sh) que no deben tapar
# las dependencias del venv local.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "python"))
