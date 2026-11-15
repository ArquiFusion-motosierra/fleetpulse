"""Idempotencia de consumidores con fp-inbox: item `{message_id, consumer, processed_at, ttl}`."""
from __future__ import annotations

import time

from botocore.exceptions import ClientError

from fplib import clock


def _client():
    import boto3
    return boto3.client("dynamodb")


def claim(table: str, message_id: str, consumer: str, ttl_days: int = 7) -> bool:
    """True la primera vez que se ve `message_id`; False si ya fue reclamado."""
    try:
        _client().put_item(
            TableName=table,
            Item={
                "message_id": {"S": message_id},
                "consumer": {"S": consumer},
                "processed_at": {"S": clock.iso(clock.now())},
                "ttl": {"N": str(int(time.time()) + ttl_days * 86400)},
            },
            ConditionExpression="attribute_not_exists(message_id)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
