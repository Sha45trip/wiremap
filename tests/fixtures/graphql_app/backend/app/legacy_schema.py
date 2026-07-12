import graphene


class Query(graphene.ObjectType):
    legacy_stats = graphene.Field(graphene.String)

    def resolve_legacy_stats(self, info):
        # graphene resolve_ prefix -> field legacyStats
        return "{}"

    def not_a_resolver(self):
        # near-miss: no resolve_ prefix, no decorator
        return None
