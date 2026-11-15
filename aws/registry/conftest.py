import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# fplib de la layer fp-shared (igual que aws/shared/conftest.py: al final para no tapar el venv)
sys.path.append(os.path.join(HERE, "..", "shared", "python"))
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
