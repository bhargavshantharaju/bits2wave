"""Provision or rotate one operator account using an interactive password prompt."""

import argparse
import getpass
import re
import sqlite3
import uuid

from werkzeug.security import generate_password_hash

from backend import app as app_module


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username",required=True)
    parser.add_argument("--display-name")
    args=parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,100}",args.username):
        parser.error("username must be 3–100 safe characters")
    password=getpass.getpass("Operator password (minimum 12 characters): ")
    confirmation=getpass.getpass("Confirm operator password: ")
    if len(password)<12 or password!=confirmation:
        parser.error("passwords must match and contain at least 12 characters")
    app_module.init_db()
    conn=sqlite3.connect(app_module.DB_PATH)
    try:
        existing=conn.execute("SELECT id,role FROM users WHERE username=?",(args.username,)).fetchone()
        password_hash=generate_password_hash(password,method="scrypt")
        if existing:
            if existing[1] != "operator":
                raise SystemExit("refusing to convert an existing non-operator account; choose a new operator username")
            conn.execute("UPDATE users SET role='operator',full_name=?,password_hash=?,session_version=session_version+1,auth_failed_attempts=0,auth_locked_until=NULL WHERE id=?",
                         (args.display_name or args.username,password_hash,existing[0]))
            conn.execute("INSERT INTO security_events(actor_id,event,outcome,source_hash,detail) VALUES(?,?,?,?,?)",
                         (existing[0],"operator_credential_rotation","success","local-cli","all prior sessions revoked"))
        else:
            conn.execute("INSERT INTO users(identity_id,username,full_name,phone,role,password_hash) VALUES(?,?,?,'','operator',?)",
                         (str(uuid.uuid4()),args.username,args.display_name or args.username,password_hash))
            operator_id=conn.execute("SELECT id FROM users WHERE username=?",(args.username,)).fetchone()[0]
            conn.execute("INSERT INTO security_events(actor_id,event,outcome,source_hash,detail) VALUES(?,?,?,?,?)",
                         (operator_id,"operator_provision","success","local-cli","new local operator account"))
        conn.commit()
    finally:
        conn.close()
    print("Operator account provisioned. No password was written to the database in plaintext.")


if __name__=="__main__":
    main()
