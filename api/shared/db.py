import os
from azure.cosmos import CosmosClient

_client = None
_container = None

def get_container():
    global _client, _container
    if _container is None:
        connection_string = os.environ["COSMOS_CONNECTION_STRING"]
        _client = CosmosClient.from_connection_string(connection_string)
        database = _client.get_database_client("job-evaluator")
        _container = database.get_container_client("queue")
    return _container
