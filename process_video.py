import logging
import json
import socket
import os
from quart import Quart, request, Response
from quart.logging import default_handler, serving_handler

from record_upload_manager import RecordUploadManager


def get_free_port():
    sock = socket.socket()
    sock.bind(('', 0))
    _, port = sock.getsockname()
    sock.close()
    return port


port = get_free_port()
app = Quart(__name__)

logging.basicConfig(level=logging.INFO if os.environ.get(
    "RECORDER_DEBUG") is None else logging.DEBUG, format=LOG_FORMAT, datefmt=DATE_FORMAT)
logging.config.dictConfig({
    "version": 1,
    "loggers": {
        "quart.app": {
            "level": "ERROR"
        },
        "quart.serving": {
            "level": "ERROR"
        }
    }
})
logging.getLogger('quart.app').removeHandler(default_handler)
logging.getLogger('quart.serving').removeHandler(serving_handler)
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

record_upload_manager = RecordUploadManager(
    port, "./recorder_config.yaml", "recorder_save.yaml")


@app.route('/process_video', methods=['POST'])
async def respond_process():
    json_request = await request.json
    logging.debug(json.dumps(json_request))
    await record_upload_manager.handle_update(json_request)
    return Response(response="", status=200)


if __name__ == "__main__":
    logging.info("webhook litsening on port %d", port)
    app.run(port=port)
