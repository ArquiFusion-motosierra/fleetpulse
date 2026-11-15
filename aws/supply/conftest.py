import os
import sys

# Raiz del servicio (supply/) + fplib de la layer bajo ../shared/python (al final, como en planning).
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(HERE)
sys.path.append(os.path.join(HERE, "..", "shared", "python"))
