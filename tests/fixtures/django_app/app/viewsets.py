class ItemViewSet:
    # DRF router expands registered viewsets by defined action
    def list(self, request):
        return []

    def retrieve(self, request, pk=None):
        return {}
