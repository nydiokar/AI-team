def waiting(conn):
    return conn.execute('SELECT id FROM main."agent_inbox" WHERE state != ?', ("acked",)).fetchall()
