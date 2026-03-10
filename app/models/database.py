from fastapi import Request


async def get_db(request: Request):
    async with request.app.state.db_session() as session:
        yield session
