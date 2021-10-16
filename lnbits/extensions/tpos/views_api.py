import httpx
import json

from quart import g, jsonify, request
from http import HTTPStatus
from urllib.parse import urlparse

from lnbits.core.crud import get_user, get_wallet
from lnbits.core.services import create_invoice, check_invoice_status
from lnbits.decorators import api_check_wallet_key, api_validate_post_request
from lnbits import lnurl

from . import tpos_ext
from .crud import create_tpos, get_tpos, get_tposs, delete_tpos


@tpos_ext.route("/api/v1/tposs", methods=["GET"])
@api_check_wallet_key("invoice")
async def api_tposs():
    wallet_ids = [g.wallet.id]
    if "all_wallets" in request.args:
        wallet_ids = (await get_user(g.wallet.user)).wallet_ids

    return (
        jsonify([tpos._asdict() for tpos in await get_tposs(wallet_ids)]),
        HTTPStatus.OK,
    )


@tpos_ext.route("/api/v1/tposs", methods=["POST"])
@api_check_wallet_key("invoice")
@api_validate_post_request(
    schema={
        "name": {"type": "string", "empty": False, "required": True},
        "currency": {"type": "string", "empty": False, "required": True},
    }
)
async def api_tpos_create():
    tpos = await create_tpos(wallet_id=g.wallet.id, **g.data)
    return jsonify(tpos._asdict()), HTTPStatus.CREATED


@tpos_ext.route("/api/v1/tposs/<tpos_id>", methods=["DELETE"])
@api_check_wallet_key("admin")
async def api_tpos_delete(tpos_id):
    tpos = await get_tpos(tpos_id)

    if not tpos:
        return jsonify({"message": "TPoS does not exist."}), HTTPStatus.NOT_FOUND

    if tpos.wallet != g.wallet.id:
        return jsonify({"message": "Not your TPoS."}), HTTPStatus.FORBIDDEN

    await delete_tpos(tpos_id)

    return "", HTTPStatus.NO_CONTENT


@tpos_ext.route("/api/v1/tposs/<tpos_id>/invoices/", methods=["POST"])
@api_validate_post_request(
    schema={"amount": {"type": "integer", "min": 1, "required": True}}
)
async def api_tpos_create_invoice(tpos_id):
    tpos = await get_tpos(tpos_id)

    if not tpos:
        return jsonify({"message": "TPoS does not exist."}), HTTPStatus.NOT_FOUND

    try:
        payment_hash, payment_request = await create_invoice(
            wallet_id=tpos.wallet,
            amount=g.data["amount"],
            memo=f"{tpos.name}",
            extra={"tag": "tpos"},
        )
    except Exception as e:
        return jsonify({"message": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR

    return (
        jsonify({"payment_hash": payment_hash, "payment_request": payment_request}),
        HTTPStatus.CREATED,
    )


@tpos_ext.route("/api/v1/tposs/<tpos_id>/invoices/<payment_hash>", methods=["GET"])
async def api_tpos_check_invoice(tpos_id, payment_hash):
    tpos = await get_tpos(tpos_id)

    if not tpos:
        return jsonify({"message": "TPoS does not exist."}), HTTPStatus.NOT_FOUND

    try:
        status = await check_invoice_status(tpos.wallet, payment_hash)
        is_paid = not status.pending
    except Exception as exc:
        print(exc)
        return jsonify({"paid": False}), HTTPStatus.OK

    if is_paid:
        wallet = await get_wallet(tpos.wallet)
        payment = await wallet.get_payment(payment_hash)
        await payment.set_pending(False)

        return jsonify({"paid": True}), HTTPStatus.OK

    return jsonify({"paid": False}), HTTPStatus.OK


@tpos_ext.route("/api/v1/tposs/<tpos_id>/lnurlw", methods=["POST"])
@api_validate_post_request(
        schema={
            "payment_request": {"type": "string", "empty": False, "required": True},
            "lnurl": {"type": "string", "empty": False, "required": True}
        }
)
async def api_tpos_lnurl(tpos_id):
    tpos = await get_tpos(tpos_id)
    if not tpos:
        return jsonify({"message": "TPoS does not exist."}), HTTPStatus.NOT_FOUND

    try:
        url = lnurl.decode(g.data["lnurl"])
        domain = urlparse(url).netloc
    except Exception:
        return jsonify({"message": "invalid lnurl"}, HTTPStatus.BAD_REQUEST)

    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=5)
        if r.is_error:
            return (
                jsonify({"domain": domain, "message": "failed to get parameters"}),
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    try:
        data = json.loads(r.text)
    except json.decoder.JSONDecodeError:
        return (
            jsonify(
                {
                    "domain": domain,
                    "message": f"got invalid response '{r.text[:200]}'",
                }
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )

    tag = data["tag"]
    if tag == "withdrawRequest":
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(
                    data["callback"],
                    params={
                        "pr": g.data["payment_request"],
                        "k1": data["k1"],
                    },
                    timeout=10,
                )
                if r.is_error:
                    lnurl_response = r.text
                else:
                    print(f"RESPONSE: {r.text}")
                    resp = json.loads(r.text)
                    if resp["status"] != "OK":
                        lnurl_response = resp["reason"]
                    else:
                        lnurl_response = True

            except (httpx.ConnectError, httpx.RequestError):
                lnurl_response = False

            return jsonify({"lnurl_response": lnurl_response})

    return jsonify({"message": "Not a withdraw request"}, HTTPStatus.SERVICE_UNAVAILABLE)
