"""LIMIT 2 is intentional in MeshDB.resolve_dispatch_requester: distinguish one from many."""

def append_link(conn, session, role):
    conn.execute("INSERT INTO flow_links (session, role) VALUES (?, ?)", (session, role))


def is_unique_requester(conn):
    candidates = conn.execute("SELECT session FROM flow_links ORDER BY id LIMIT 2").fetchall()
    return len(candidates) == 1
