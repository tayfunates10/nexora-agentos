from uuid import uuid4

from psycopg.types.json import Jsonb


async def append_run_event(connection, workspace_id, run_id, event_type, payload=None):
    """Caller must hold the agent_runs row lock before appending."""
    result = await connection.execute(
        """SELECT COALESCE(MAX(event_no), 0) + 1 AS event_no
           FROM agent_run_events WHERE run_id=%s""",
        (run_id,),
    )
    event_no = (await result.fetchone())["event_no"]
    await connection.execute(
        """INSERT INTO agent_run_events
           (id,workspace_id,run_id,event_no,event_type,payload)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (uuid4(), workspace_id, run_id, event_no, event_type, Jsonb(payload or {})),
    )
    return event_no
