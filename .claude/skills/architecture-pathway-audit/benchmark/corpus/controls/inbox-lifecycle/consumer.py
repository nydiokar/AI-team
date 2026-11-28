from owner import pending_for


def waiting(conn):
    return pending_for(conn)
