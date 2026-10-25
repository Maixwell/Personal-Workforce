"""OpenAI model request for Max Smart."""


def get_model_response(client, standing_context, api_input):
    """Send one Responses API request and return the model's text response."""
    response = client.responses.create(
        model="gpt-5.6-sol",
        instructions=standing_context,
        input=api_input,
        store=False,
    )

    return response.output_text
