from logging import getLogger
from redis import StrictRedis
from typing import Dict, List, Tuple
from CloudHarvestCoreTasks.tasks import BaseTaskChain, TaskStatusCodes

logger = getLogger('harvest')


class JobQueue(Dict[str, BaseTaskChain]):
    """
    The JobQueue class is responsible for checking the Redis queue for new tasks and adding them to the queue. It also
    reports the status of any running task chains to the harvest-agents silo.
    """
    from .api import Api

    def __init__(self,
                 api: Api,
                 accepted_chain_priorities: List[int],
                 chain_progress_reporting_interval_seconds: int,
                 chain_task_restrictions: List[str],
                 chain_timeout_seconds: int,
                 queue_check_interval_seconds: int,
                 max_chains: int,
                 reporting_interval_seconds: int,
                 *args, **kwargs):

        """
        The JobQueue class is responsible for checking the Redis queue for new tasks and adding them to the queue. It also
        manages the queue and provides methods to interact with it.

        Parameters
        api_host (str): The host of the API.
        api_port (int): The port of the API.
        api_token (str): The token to authenticate with the API.
        accepted_chain_priorities (List[int]): A list of accepted chain priorities.
        chain_progress_reporting_interval_seconds (int): The interval in seconds for reporting chain progress.
        chain_task_restrictions (List[str]): A list of task restrictions for the chains.
        chain_timeout_seconds (int): The timeout in seconds for each chain.
        queue_check_interval_seconds (int): The interval in seconds for checking the queue.
        max_running_chains (int): The maximum number of running chains.
        reporting_interval_seconds (int): The interval in seconds for reporting.

        Additional Parameters:
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments.

        """

        super().__init__()

        # Api configuration
        self.api = api

        # Silo configurations retrieved from the API
        self._silos = {}

        # Queue configuration
        self.accepted_chain_priorities = accepted_chain_priorities
        self.chain_progress_reporting_interval_seconds = chain_progress_reporting_interval_seconds
        self.chain_task_restrictions = chain_task_restrictions
        self.chain_timeout_seconds = chain_timeout_seconds
        self.queue_check_interval_seconds = queue_check_interval_seconds
        self.max_chains = max_chains
        self.reporting_interval_seconds = reporting_interval_seconds

        # Threads
        self._reporting_thread = None
        self._check_queue_thread = None
        self._task_chain_threads = {}

        # Programmatic attributes
        from datetime import datetime, timezone
        self.start_time = datetime.now(tz=timezone.utc)
        self.status = TaskStatusCodes.initialized
        self.stop_time = None

    def _on_chain_complete(self, task_chain_id: str):
        """
        A callback that is called when a task chain completes. It sends the chain's final results to the harvest-agent-results
        silo, then removes the task chain from the JobQueue.
        :return:
        """
        from CloudHarvestCoreTasks.silos import get_silo
        # Update the harvest-tasks silo with the task chain status
        harvest_tasks_silo = get_silo('harvest-tasks')

        # Send the final results to the harvest-task-results silo
        harvest_task_results_silo = get_silo('harvest-task-results')



        # Remove the task chain from the JobQueue
        self.pop(task_chain_id, None)

    def _thread_check_queue(self):
        """
        A thread that checks the Redis queue for new tasks and adds them to the JobQueue.
        :return:
        """
        from CloudHarvestCoreTasks.silos import get_silo
        from threading import Thread
        from time import sleep

        # If there is room in the queue, start adding tasks from the Redis queue
        while not self.is_queue_full and self.status != TaskStatusCodes.terminating:
            silo: StrictRedis = get_silo('harvest-task-queue').connect()
            oldest_task = get_oldest_task_from_queue(silo, self.accepted_chain_priorities)

            if oldest_task:
                from json import loads
                loaded_template = loads(oldest_task[0])
                self.get_task_chain_from_template(task_template_name=loaded_template['task_template_name'],
                                                  user_parameters=loaded_template['user_parameters'],
                                                  tags=loaded_template.get('tags'),
                                                  uuid=loaded_template.get('uuid'))

            sleep(self.queue_check_interval_seconds)

    @property
    def is_queue_full(self) -> bool:
        """
        Returns a boolean indicating whether the queue is full.
        :return:
        """
        return len(self.keys()) >= self.max_chains

    def _thread_reporting(self):
        """
        A thread that reports the progress of the task chains to the API.
        :return:
        """

        from json import dumps
        from redis import StrictRedis
        from time import sleep

        while True:
            reporting_silo = StrictRedis(**self._silos.get('harvest-tasks'))

            try:

                for task_chain_id, task_chain in list(self.items()):
                    # Report the progress of the task chain to the API
                    reporting_silo.set(task_chain_id, dumps(task_chain.detailed_progress()))

                    # Automatically expire the key after 10 reporting intervals
                    reporting_silo.expire(task_chain_id, self.reporting_interval_seconds * 10)

                    # Escape the loop if the task chain is complete or terminating
                    if self.status in [TaskStatusCodes.complete, TaskStatusCodes.terminating]:
                        break

            except Exception as e:
                logger.error(f'Error while reporting chain progress: {e.args}')

            else:
                logger.info('Chain progress reported.')

            finally:
                sleep(self.reporting_interval_seconds)

    def detailed_status(self) -> dict:
        """
        Returns detailed status information about the JobQueue.
        :return:
        """

        from CloudHarvestCoreTasks.tasks import TaskStatusCodes

        result = {
            'chain_status': {
                str(status_code): sum(1 for task in self.values() if task.status == status_code)
                for status_code in TaskStatusCodes
            },
            'duration': self.duration,
            'max_chains': self.max_chains,
            'start_time': self.start_time,
            'status': self.status,
            'stop_time': self.stop_time,
            'total_chains_in_queue': len(self)
        }

        return result

    @property
    def duration(self) -> float:
        """
        Returns the duration of the JobQueue in seconds.
        :return:
        """
        from datetime import datetime, timezone

        if self.stop_time:
            result = (self.stop_time - self.start_time).total_seconds()

        else:
            result = (datetime.now(tz=timezone.utc) - self.start_time).total_seconds()

        return result

    def get_chain_status(self, task_chain_id: str) -> dict:
        """
        Retrieves the status of a task chain.
        :return:
        """

        task_chain: BaseTaskChain = self.get(task_chain_id)

        if task_chain is None:
            raise ValueError(f'Task chain with ID {task_chain_id} not found.')

        return {
            task_chain_id: task_chain.detailed_progress()
        }

    @staticmethod
    def prepare_redis_payload(dictionary: dict) -> dict:
        """
        Prepares a dictionary to be stored in Redis by converting incompatible types to strings.
        :param dictionary: The dictionary to prepare.
        :return: The prepared dictionary.
        """
        from flatten_json import flatten_preserve_lists, unflatten

        separator = '.'

        flat_dictionary = flatten_preserve_lists(dictionary, separator=separator)

        for key, value in flat_dictionary.items():
            if not isinstance(value, (str, int, float, bool)):
                flat_dictionary[key] = str(value)

        return unflatten(flat_dictionary, separator=separator)

    def start(self) -> dict:
        """
        Starts the job queue process.
        :return: A dictionary containing the result and message.
        """

        logger.info('Starting the JobQueue.')

        # Set the queue status to 'running'
        self.status = JobQueueStatusCodes.running

        # Reset the stop time
        self.stop_time = None

        try:

            # Start the reporting and queue check threads
            from threading import Thread
            self._reporting_thread = Thread(target=self._thread_reporting, daemon=True)
            self._check_queue_thread = Thread(target=self._thread_check_queue, daemon=True)

        except Exception as ex:
            message = f'Error while starting the JobQueue: {ex.args}'
            logger.error(message)
            self.status = JobQueueStatusCodes.error

        else:
            message = 'JobQueue started successfully.'

        return {
            'result': self.status,
            'message': message
        }

    def get_task_chain_from_template(self,
                                     task_template_name: str,
                                    user_parameters: dict,
                                    tags: List[str] = None,
                                    uuid: str = None) -> BaseTaskChain:
        """
        Instantiates a task chain from a task template and starts it.

        Arguments
        task_template_name (str): The name of the task template.
        user_parameters (dict): The user parameters to pass to the task chain.
        uuid (str): The UUID of the task chain.
        """

        from CloudHarvestCorePluginManager.registry import Registry
        from CloudHarvestCoreTasks.tasks.factories import task_chain_from_dict

        # Retrieve the task template from the registry
        template = Registry.find(result_key='*',
                                      category='template',
                                      name=task_template_name,
                                      tags=tags)[0]

        task_chain = task_chain_from_dict(template=template, **user_parameters)

        # Override the UUID if provided by the API
        if uuid:
            task_chain.uuid = uuid

        # Add the task chain to the JobQueue
        self[task_chain.uuid] = task_chain

        return task_chain

    def stop(self, finish_running_jobs: bool = True, timeout: int = 60) -> dict:
        """
        Terminates the queue and reporting threads.
        :param finish_running_jobs: A boolean indicating whether to finish running jobs.
        :param timeout: The timeout in seconds to wait for running jobs to complete.
        :return:
        """
        from datetime import datetime, timezone

        logger.warning('Stopping the JobQueue.')
        self.status = JobQueueStatusCodes.stopping

        # Prevents the JobQueue from starting new tasks
        self._thread_check_queue.join()

        if not finish_running_jobs:
            logger.info('Ordering TaskChains to terminate.')

            # Notify the threads to stop
            for task_chain_id, task_chain in self.items():
                task_chain.terminate()

        timeout_start_time = datetime.now()

        # Wait for the task chains to complete
        from CloudHarvestCoreTasks.tasks import TaskStatusCodes
        while (datetime.now() - timeout_start_time).total_seconds() < timeout:
            if all([task_chain.status not in (TaskStatusCodes.initialized, TaskStatusCodes.running) for task_chain in self.values()]):
                logger.info('All task chains have completed.')
                self.status = JobQueueStatusCodes.stopped
                result = True
                break

        else:
            result = False

        # Record the stop time
        self.stop_time = datetime.now(tz=timezone.utc)

        return {
            'result': result,
            'message': 'All task chains have completed.' if result else 'Timeout exceeded while waiting for task chains to complete.'
        }

def get_oldest_task_from_queue(silo: StrictRedis, accepted_chain_priorities: List[int]) -> Tuple[str, str]:
    """
    Retrieves the oldest task from the queue.
    :param silo: The Redis silo to retrieve the task from.
    :param accepted_chain_priorities: A list of accepted chain priorities.
    :return: The oldest task from the Redis database as a tuple of task_id and task.
    """

    # harvest-task-queue format:
    #   name: {priority}:{task_chain_id}
    #   value: {task_chain_json_payload}

    for priority in accepted_chain_priorities:
        # Retrieve the first 100 tasks from the queue where the priority matches
        redis_names = silo.scan_iter(match=f'{priority}:*', count=100)

        for redis_name in redis_names:
            # Try to pop the first task from the queue
            task_id = redis_name.split(':')[1]
            task = silo.lpop(redis_name)

            if task:
                return task_id, task


class JobQueueStatusCodes:
    complete = 'complete'
    error = 'error'
    initialized = 'initialized'
    running = 'running'
    stopped = 'stopped'
    stopping = 'stopping'
    terminating = 'terminating'
