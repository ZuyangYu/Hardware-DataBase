class QueryCancelled(Exception):
    """Signal that a user cancelled an in-flight query.

    This is deliberately separate from ordinary tool failures so the turn
    worker can finish the turn as cancelled instead of recording a retrieval
    error or generating a partial answer.
    """

