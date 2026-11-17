# START HERE — Versioned Desk API contract (P1).
# Stable error codes, OpenAPI 3 document for /v1 create + decide, and helpers
# so clients can depend on `code` without scraping free-text `error` strings.
"""OpenAPI document and stable API error codes for Disbursement Desk."""
from __future__ import annotations

# Catalog — keep in sync with docs/api-contract.md
DEMO_SESSION_CHANGED = 'DEMO_SESSION_CHANGED'
OPERATOR_UNAUTHORIZED = 'OPERATOR_UNAUTHORIZED'
OPERATOR_FORBIDDEN = 'OPERATOR_FORBIDDEN'
REVIEWER_NOT_ACCEPTED = 'REVIEWER_NOT_ACCEPTED'
CONTENT_TYPE_REQUIRED = 'CONTENT_TYPE_REQUIRED'
INVALID_JSON_BODY = 'INVALID_JSON_BODY'
VALIDATION_ERROR = 'VALIDATION_ERROR'
NOT_FOUND = 'NOT_FOUND'
CONFLICT = 'CONFLICT'
IDEMPOTENCY_KEY_REQUIRED = 'IDEMPOTENCY_KEY_REQUIRED'
IDEMPOTENCY_KEY_REUSED = 'IDEMPOTENCY_KEY_REUSED'
INTERNAL_ERROR = 'INTERNAL_ERROR'
RAIL_OVER_LIMIT = 'RAIL_OVER_LIMIT'
RAIL_WINDOW_CLOSED = 'RAIL_WINDOW_CLOSED'
STRIPE_SEND_BLOCKED = 'STRIPE_SEND_BLOCKED'

ERROR_CODES = frozenset({
    DEMO_SESSION_CHANGED,
    OPERATOR_UNAUTHORIZED,
    OPERATOR_FORBIDDEN,
    REVIEWER_NOT_ACCEPTED,
    CONTENT_TYPE_REQUIRED,
    INVALID_JSON_BODY,
    VALIDATION_ERROR,
    NOT_FOUND,
    CONFLICT,
    IDEMPOTENCY_KEY_REQUIRED,
    IDEMPOTENCY_KEY_REUSED,
    INTERNAL_ERROR,
    RAIL_OVER_LIMIT,
    RAIL_WINDOW_CLOSED,
    STRIPE_SEND_BLOCKED,
})

OPENAPI_VERSION = '3.0.3'
API_VERSION = '1.0.0'


def openapi_document(public_origin=None):
    """Minimal OpenAPI 3 document for the versioned create/decide surface."""
    servers = [{'url': public_origin}] if public_origin else [{'url': '/'}]
    return {
        'openapi': OPENAPI_VERSION,
        'info': {
            'title': 'Disbursement Desk API',
            'version': API_VERSION,
            'description': (
                'Fictional payouts only. Versioned create/decide under /v1. '
                'Legacy /payouts/* routes remain for the Desk UI.'
            ),
        },
        'servers': servers,
        'paths': {
            '/v1/payouts': {
                'post': {
                    'operationId': 'createPayout',
                    'summary': 'Create one synthetic payout (idempotent)',
                    'parameters': [{
                        'name': 'Idempotency-Key',
                        'in': 'header',
                        'required': True,
                        'schema': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                    }],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {'$ref': '#/components/schemas/CreatePayoutRequest'},
                            },
                        },
                    },
                    'responses': {
                        '201': {'description': 'Created',
                                'content': {'application/json': {
                                    'schema': {'$ref': '#/components/schemas/CreatePayoutResponse'}}}},
                        '200': {'description': 'Idempotent replay'},
                        '422': {'description': 'Validation error',
                                'content': {'application/json': {
                                    'schema': {'$ref': '#/components/schemas/Error'}}}},
                        '409': {'description': 'Conflict',
                                'content': {'application/json': {
                                    'schema': {'$ref': '#/components/schemas/Error'}}}},
                    },
                },
            },
            '/v1/payouts/{payout_id}/decisions': {
                'post': {
                    'operationId': 'decidePayout',
                    'summary': 'Record a decide action bound to the signed-in operator',
                    'parameters': [
                        {
                            'name': 'payout_id',
                            'in': 'path',
                            'required': True,
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'Idempotency-Key',
                            'in': 'header',
                            'required': True,
                            'schema': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                        },
                    ],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {'$ref': '#/components/schemas/DecideRequest'},
                            },
                        },
                    },
                    'responses': {
                        '201': {'description': 'Decision recorded'},
                        '200': {'description': 'Idempotent replay'},
                        '403': {'description': 'Operator forbidden',
                                'content': {'application/json': {
                                    'schema': {'$ref': '#/components/schemas/Error'}}}},
                        '409': {'description': 'Illegal status transition'},
                        '422': {'description': 'Validation / reviewer rejected'},
                    },
                },
            },
            '/openapi.json': {
                'get': {
                    'operationId': 'getOpenApi',
                    'summary': 'OpenAPI 3 document (no demo session)',
                    'responses': {'200': {'description': 'OpenAPI document'}},
                },
            },
        },
        'components': {
            'schemas': {
                'Error': {
                    'type': 'object',
                    'required': ['error'],
                    'properties': {
                        'error': {'type': 'string'},
                        'code': {
                            'type': 'string',
                            'enum': sorted(ERROR_CODES),
                        },
                    },
                },
                'CreatePayoutRequest': {
                    'type': 'object',
                    'required': ['payee_name', 'payee_account', 'amount_cents'],
                    'properties': {
                        'payout_id': {'type': 'string'},
                        'payee_name': {'type': 'string'},
                        'payee_account': {'type': 'string', 'description': 'DEMO-… account id'},
                        'amount_cents': {'type': 'integer', 'minimum': 1},
                        'rail': {
                            'type': 'string',
                            'enum': ['ACH', 'SAME_DAY_ACH', 'RTP', 'FEDNOW', 'WIRE'],
                        },
                        'bank_details_changed_at': {'type': 'string'},
                        'routing_number': {'type': 'string'},
                    },
                },
                'CreatePayoutResponse': {
                    'type': 'object',
                    'properties': {
                        'payout': {'type': 'object'},
                        'status': {'type': 'string'},
                        'changed': {'type': 'boolean'},
                        'replayed': {'type': 'boolean'},
                    },
                },
                'DecideRequest': {
                    'type': 'object',
                    'required': ['action'],
                    'properties': {
                        'action': {
                            'type': 'string',
                            'enum': ['approve', 'hold', 'return', 'clear', 'block'],
                        },
                        'reason': {'type': 'string', 'maxLength': 500},
                    },
                },
            },
        },
    }
