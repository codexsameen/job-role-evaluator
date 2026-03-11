import os
from azure.cosmos import CosmosClient, PartitionKey

_client = None
_container = None
_profiles_container = None

def _get_client():
    global _client
    if _client is None:
        connection_string = os.environ["COSMOS_CONNECTION_STRING"]
        _client = CosmosClient.from_connection_string(connection_string)
    return _client

def get_container():
    global _container
    if _container is None:
        database = _get_client().get_database_client("job-evaluator")
        _container = database.get_container_client("queue")
    return _container

def get_profiles_container():
    global _profiles_container
    if _profiles_container is None:
        client = _get_client()
        database = client.get_database_client("job-evaluator")
        database.create_container_if_not_exists(
            id="profiles",
            partition_key=PartitionKey(path="/userId"),
        )
        _profiles_container = database.get_container_client("profiles")
    return _profiles_container
