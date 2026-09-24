"""Service plugin registry."""
from app.services.openai_service import OpenAIService
from app.services.fireflies_service import FirefliesService
from app.services.claude_service import ClaudeService

SERVICE_REGISTRY = {
    OpenAIService.slug: OpenAIService,
    FirefliesService.slug: FirefliesService,
    ClaudeService.slug: ClaudeService,
}


def get_service_class(slug: str):
    try:
        return SERVICE_REGISTRY[slug]
    except KeyError as exc:
        raise ValueError(f"Unknown service '{slug}'. Known services: {list(SERVICE_REGISTRY)}") from exc
