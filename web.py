import logging
from os import getenv
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv
from flask import Flask, request
from pythonjsonlogger import jsonlogger
import requests
import json

LOG_FIELDS = ["levelname"]

logger = logging.getLogger()
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    reserved_attrs=[a for a in jsonlogger.RESERVED_ATTRS if a not in LOG_FIELDS],
    timestamp=True,
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

try:
    load_dotenv(find_dotenv(), override=True)
except Exception:
    logger.info("No .env")

app = Flask(__name__)

# need better logging
# need a datastore
# need guincorn

# need privacy policy
# need terms of service

# get opt-in via web hook
# save opt-ins to (somewhere)
# handle opt-outs

"""
opt in flow:
    receive message
        save userid
        save message
        save timestamp
    respond
"""

VERIFY_TOKEN = "32912933-981d-4002-bf45-c4b491686385"
FACEBOOK_PAGE_TOKEN = getenv("FACEBOOK_PAGE_TOKEN")


@app.route("/hello_from_messenger", methods=["GET"])
def hello():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("WEBHOOK VERIFIED")
            return challenge, 200
        else:
            return "oh no you did not", 403

    return "hey now"


@dataclass
class ReceivedMessage:
    sender: str
    text: str


def respond_to_message(received_message):
    body = {
        "recipient": {"id": received_message.sender},
        "message": {
            "text": "Do you want us to send you a facebook message when there are available COVID vaccination appointments nearby?",
            "quick_replies": [
                {
                    "content_type": "text",
                    "title": "YES!",
                    "payload": json.dumps({"opt_in": {"id": received_message.sender}}),
                },
                {
                    "content_type": "text",
                    "title": "NOPE!",
                    "payload": json.dumps(
                        {"declined": {"id": received_message.sender}}
                    ),
                },
            ],
        },
        "messaging_type": "RESPONSE",
    }

    headers = {"Content-Type": "application/json"}

    url = "https://graph.facebook.com/v10.0/me/messages"

    params = {"access_token": FACEBOOK_PAGE_TOKEN}

    response = requests.request("POST", url, headers=headers, json=body, params=params)

    print(response.text)


@app.route("/hello_from_messenger", methods=["post"])
def posted():
    message = request.get_json()
    entries = message.get("entry", [])
    for entry in entries:
        messaging = entry.get("messaging")
        if messaging:
            for item in messaging:
                sender = item.get("sender", {}).get("id")
                text = item.get("message", {}).get("text")
                print(messaging)
                respond_to_message(ReceivedMessage(sender, text))
    return "yo"


if __name__ == "__main__":
    app.run(port=9000)
