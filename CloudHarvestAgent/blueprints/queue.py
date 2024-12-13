"""
The queue blueprint is responsible for managing the job queue.
"""

from CloudHarvestCoreTasks.blueprints import HarvestAgentBlueprint
from flask import Response, jsonify, request
from .home import not_implemented_error


# Blueprint Configuration
queue_blueprint = HarvestAgentBlueprint(
    'queue_bp', __name__,
    url_prefix='/queue'
)

@queue_blueprint.route(rule='inject', methods=['POST'])
def inject():
    """
    Accepts a serialized TaskChain, puts it in the JobQueue, and immediately starts it. This operation bypasses the
    JobQueue's scheduling and limit mechanisms. This is useful when a task chain needs to be executed immediately.

    :return: uuid of the instantiated TaskChain
    """

    # TODO: Implement this method

    # from ..app import CloudHarvestAgent
    #
    # incoming_request = request.get_json()

    return not_implemented_error()


@queue_blueprint.route(rule='start', methods=['GET'])
def start() -> Response:
    """
    Starts the job queue.
    """
    from ..app import CloudHarvestNode

    result = CloudHarvestNode.job_queue.start()

    return jsonify(result)


@queue_blueprint.route(rule='stop', methods=['GET'])
def stop() -> Response:
    """
    Stops the job queue.
    """
    from ..app import CloudHarvestNode

    result = CloudHarvestNode.job_queue.stop()

    return jsonify(result)


@queue_blueprint.route(rule='status', methods=['GET'])
def status() -> Response:
    """
    Returns a detailed status of the job queue.
    """
    from ..app import CloudHarvestNode

    return jsonify(CloudHarvestNode.job_queue.detailed_status())


# Escalation of tasks is done at the API level
# @queue_blueprint.route(rule='escalate/<task_chain_id>', methods=['GET'])
# def escalate(task_chain_id: str) -> Response:
#     pass
