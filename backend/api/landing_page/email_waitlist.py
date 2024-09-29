from textwrap import dedent

from django.contrib import messages
from login_required import login_not_required

from backend.models import InvoiceProduct
from backend.service import BOTO3_HANDLER
from backend.types.requests import WebRequest

from django.http import HttpResponse


@login_not_required
def join_waitlist_endpoint(request: WebRequest):
    email_address = request.POST.get("email", "")
    name = request.POST.get("name", "")

    if not email_address:
        return HttpResponse(status=400)

    if not BOTO3_HANDLER.initiated:
        return HttpResponse(status=500)

    BOTO3_HANDLER.dynamodb_client.put_item(TableName="myfinances-emails", Item={"email": {"S": email_address}, "name": {"S": name}})

    content = """
        <div class='text-success'>
            Successfully registered! Expect some discounts and updates as we progress in our journey :)
        </div>
    """

    return HttpResponse(status=200, content=dedent(content).strip())
