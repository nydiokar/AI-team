"""F1 from MeshDB at 49c4dfa62: apply the spec filter after the oldest 5,000 rows."""

def append_review(conn, spec, passed):
    conn.execute("INSERT INTO flow_events (spec, passed) VALUES (?, ?)", (spec, passed))


def latest_review(conn, spec):
    reviews = conn.execute("SELECT spec, passed FROM flow_events ORDER BY id LIMIT 5000")
    return next(((passed,) for name, passed in reversed(list(reviews)) if name == spec), None)
