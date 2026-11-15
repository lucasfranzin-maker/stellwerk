"""Alembic-Umgebung. Die Engine kommt von Store.migrate(), es gibt keine alembic.ini.

Offline (stellwerk migrate --sql postgresql) wird nur das SQL ausgegeben, etwa für die Datenbankverwaltung im Haus.
"""
from alembic import context

from stellwerk.db import Base

if context.is_offline_mode():
    context.configure(dialect_name=context.config.attributes["dialekt"], target_metadata=Base.metadata,
                      literal_binds=True, output_buffer=context.config.attributes["ausgabe"])
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = context.config.attributes["engine"]
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=engine.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
