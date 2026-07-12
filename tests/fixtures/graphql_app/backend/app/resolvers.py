import strawberry

from .services import load_user


@strawberry.type
class Query:
    @strawberry.field
    def user(self, id: int):
        # call graph must continue from resolvers
        return load_user(id)

    @strawberry.field
    def order_history(self):
        # snake_case -> camelCase (strawberry default): orderHistory
        # planted: f-string SQL in a resolver -> sql_injection_risk
        return db.execute(f"SELECT * FROM orders WHERE user = {self.uid}")

    def helper(self):
        # near-miss: no @strawberry.field decorator -> not a root field
        return None


@strawberry.type
class Mutation:
    @strawberry.field
    def create_order(self, input: dict):
        return {"id": 1}
