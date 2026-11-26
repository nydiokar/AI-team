"""Clean baseline with complete membership and a latest-applicable review."""

def append_review(conn, spec, passed):
    conn.execute("INSERT INTO flow_events (spec, passed) VALUES (?, ?)", (spec, passed))


def latest_review(conn, spec):
    return conn.execute(
        "SELECT passed FROM flow_events WHERE spec = ? ORDER BY id DESC LIMIT 1", (spec,)
    ).fetchone()


def append_link(conn, session, role):
    conn.execute("INSERT INTO flow_links (session, role) VALUES (?, ?)", (session, role))


def role_by_session(conn):
    return dict(conn.execute("SELECT session, role FROM flow_links ORDER BY id"))
