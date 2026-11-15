"""Guarda de plantilla: Outputs propios que chocan con los que Serverless genera se fusionan en un Value invalido."""
from pathlib import Path

import yaml

# Serverless v3 genera estos Outputs; HttpApiId es inocuo porque ambos valores son `Ref: HttpApi`.
FRAMEWORK_OUTPUTS = {"HttpApiId", "HttpApiUrl", "ServerlessDeploymentBucketName"}


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: {suffix: loader.construct_scalar(node)
                                                                  if isinstance(node, yaml.ScalarNode)
                                                                  else loader.construct_sequence(node)})


def test_custom_outputs_do_not_collide_with_framework_outputs():
    cfg = yaml.load((Path(__file__).resolve().parents[1] / "serverless.yml").read_text(), Loader=_Loader)
    ours = set(cfg["resources"]["Outputs"])
    assert ours & FRAMEWORK_OUTPUTS <= {"HttpApiId"}
    assert cfg["resources"]["Outputs"]["HttpApiId"]["Value"] == {"Ref": "HttpApi"}
