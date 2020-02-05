from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class Response(BaseModel):
    code: str


class GatewayLog(BaseModel):
    timed_out: Optional[bool]


class Funding(BaseModel):
    gateway_log: Optional[GatewayLog]


class GpaOrder(BaseModel):
    funding: Optional[Funding]


class Transaction(BaseModel):
    token: Optional[str]
    response: Optional[Response]
    gpa_order: Optional[GpaOrder]
    type: str
    state: Optional[str]
    user_token: Optional[str]


class TransactionProcessResult(BaseModel):
    transaction_token: str
    user_token: str
    process_type: str
    delivery_id: Optional[int]
    amount: Optional[int]
    card_acceptor: Optional[Dict[str, Any]]


class TransactionProcessResults(BaseModel):
    processed_results: List[TransactionProcessResult]
