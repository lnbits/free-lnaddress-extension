import json
from http import HTTPStatus
from urllib.parse import urlparse

import httpx
from fastapi import Query, Request
from lnurl import LnurlErrorResponse, LnurlPayActionResponse, LnurlPayResponse
from loguru import logger
from starlette.exceptions import HTTPException

from lnbits.core.services import create_invoice
from lnbits.utils.exchange_rates import get_fiat_rate_satoshis

from . import lnaddy_ext
from .crud import get_address_data, get_pay_link, increment_pay_link


# for .well-known/lnurlp
async def lnaddy_lnurl_response(username: str, domain: str, request: Request):
    address_data = await get_address_data(username)

    if not address_data:
        return {"status": "ERROR", "reason": "Address not found."}

    resp = {
        "tag": "payRequest",
        "callback": request.url_for(
            "lnaddy.api_lnurl_callback", link_id=address_data.id
        ),
        "metadata": await address_data.lnurlpay_metadata(domain=domain),
        "minSendable": int(address_data.min * 1000),
        "maxSendable": int(address_data.max * 1000),
    }

    logger.debug("RESP", resp)
    return resp


# for normal lnurlp api call
@lnaddy_ext.get(
    "/api/v1/lnurl/{link_id}",
    status_code=HTTPStatus.OK,
    name="lnaddy.api_lnurl_response",
)
async def api_lnurl_response(request: Request, link_id):
    link = await increment_pay_link(link_id, served_meta=1)

    if not link:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="API response: Pay link does not exist.",
        )

    rate = await get_fiat_rate_satoshis(link.currency) if link.currency else 1

    try:
        metadata = [["text/plain", link.description]]

        resp = LnurlPayResponse(
            callback=request.url_for("lnaddy.api_lnurl_callback", link_id=link.id),
            min_sendable=round(link.min * rate) * 1000,
            max_sendable=round(link.max * rate) * 1000,
            metadata=json.dumps(metadata),
        )
        params = resp.dict()

        if link.comment_chars > 0:
            params["commentAllowed"] = link.comment_chars

        return params
    except Exception as e:
        print(e)


# for lnaddress callback
@lnaddy_ext.get(
    "/api/v1/lnurl/cb/{link_id}",
    status_code=HTTPStatus.OK,
    name="lnaddy.api_lnurl_callback",
)
async def api_lnurl_callback(request: Request, link_id, amount: int = Query(...)):
    address = await get_pay_link(link_id)
    if not address:
        return LnurlErrorResponse(reason=f'{"Address not found"}').dict()

    domain = urlparse(str(request.url)).netloc
    assert domain

    unhashed_description = await address.lnurlpay_metadata(domain=domain)
    unhashed_description = unhashed_description.encode()
    payment_hash, payment_request = await create_invoice(
        wallet_id=address.wallet,
        amount=int(amount / 1000),
        memo=address.description,
        unhashed_description=unhashed_description,
        extra={
            "tag": "lnaddy",
            "link": address.id,
            "extra": {"tag": f"Payment to {address.lnaddress}@{domain}"},
        },
    )

    success_action = address.success_action(payment_hash)
    if success_action:
        resp = LnurlPayActionResponse(
            pr=payment_request, success_action=success_action, routes=[]
        )
    else:
        resp = LnurlPayActionResponse(pr=payment_request, routes=[])

    return resp.dict()
