PENDING_SQL = "SELECT id FROM agent_inbox WHERE state = 'pending'"


def pending_for(conn):
    return conn.execute(PENDING_SQL).fetchall()


def inspect_terminal(conn):
    return conn.execute("SELECT id FROM agent_inbox WHERE state = 'dead'").fetchall()
