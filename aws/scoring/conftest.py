import os
import sys

# Raiz del servicio (scoring/) + fplib de la layer bajo ../shared/python (al final, como en ingest).
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(HERE)
sys.path.append(os.path.join(HERE, "..", "shared", "python"))


def pytest_configure(config):
    # Aqui y no en pytest.ini: CI corre todos los tests en un solo pytest desde la raiz y no lee ese ini
    config.addinivalue_line("markers", "slow: replay largo del simulador real; solo corre con FP_CALIBRATION=1")
