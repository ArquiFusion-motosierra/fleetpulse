"""Token de Step Functions dentro del enlace de aprobacion (`/approvals/{token+}`).

El task token es base64 estandar y puede traer `//`; API Gateway colapsa `//` en la ruta y Step Functions responde
TaskTimedOut con el token alterado. En el enlace va en base64 url-safe (`+` -> `-`, `/` -> `_`); `api/approvals` lo
devuelve a estandar. Un token estandar no trae `-` ni `_`, asi que los enlaces viejos siguen funcionando.
"""
_TO_LINK = str.maketrans("+/", "-_")
_FROM_LINK = str.maketrans("-_", "+/")


def token_in_link(token: str) -> str:
    return token.translate(_TO_LINK)


def token_from_link(value: str) -> str:
    return value.translate(_FROM_LINK)
