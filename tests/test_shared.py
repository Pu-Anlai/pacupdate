import aiohttp
import pytest
from aioresponses import aioresponses
from pacupdate.shared import make_aur_request


@pytest.mark.asyncio
async def test_make_aur_request():
    with aioresponses() as mock:
        mock.get(
            "https://aur.archlinux.org/rpc/v5/info?arg[]=valid-package",
            body='{"resultcount":1,"results":[{"Name": "valid-package"}],"type":"multiinfo","version":5}',
            content_type="application/json",
        )
        mock.get(
            "https://aur.archlinux.org/rpc/v5/info?arg[]=missing-package",
            body='{"resultcount":0,"results":[],"type":"multiinfo","version":5}',
            content_type="application/json",
        )
        mock.get(
            "https://aur.archlinux.org/rpc/v5/info?arg[]=invalid-json",
            body="invalid-json",
        )
        mock.get(
            "https://aur.archlinux.org/rpc/v5/info?arg[]=panic",
            exception=aiohttp.ClientError,
        )

        async with aiohttp.ClientSession() as session:
            result_valid = await make_aur_request("valid-package", session)
            assert result_valid == {"Name": "valid-package"}

            result_missing = await make_aur_request("missing-package", session)
            assert result_missing == None

            result_invalid = await make_aur_request("invalid-json", session)
            assert result_invalid == None

            result_panic = await make_aur_request("panic", session)
            assert result_panic == None
