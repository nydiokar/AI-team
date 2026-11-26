"""F2 from MeshDB and cost_read_model at 49c4dfa62: treat a display window as membership."""

def append_link(conn, session, role):
    conn.execute("INSERT INTO flow_links (session, role) VALUES (?, ?)", (session, role))


def role_by_session(conn):
    return dict(conn.execute("SELECT session, role FROM flow_links ORDER BY id LIMIT 200"))
