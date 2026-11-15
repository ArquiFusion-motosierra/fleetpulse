import os
import sys

# Raiz del servicio (planning/) + fplib de la layer bajo ../shared/python (al final, como en ingest y scoring).
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(HERE)
sys.path.append(os.path.join(HERE, "..", "shared", "python"))
