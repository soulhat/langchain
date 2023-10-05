from typing import Any, Iterator, List, Mapping, Optional

import requests
from requests.exceptions import ConnectionError

from langchain.llms.base import LLM
from langchain.llms.utils import enforce_stop_tokens
from langchain.schema.callbacks.manager import CallbackManagerForLLMRun
from langchain.schema.output import GenerationChunk


class TitanTakeoff(LLM):
    base_url: str = "http://localhost:8000"
    """Specifies the baseURL to use for the Titan Takeoff API. 
    Default = http://localhost:8000.
    """

    generate_max_length: int = 128
    """Maximum generation length. Default = 128."""

    sampling_topk: int = 1
    """Sample predictions from the top K most probable candidates. Default = 1."""

    sampling_topp: float = 1.0
    """Sample from predictions whose cumulative probability exceeds this value.
    Default = 1.0.
    """

    sampling_temperature: float = 1.0
    """Sample with randomness. Bigger temperatures are associated with 
    more randomness and 'creativity'. Default = 1.0.
    """

    repetition_penalty: float = 1.0
    """Penalise the generation of tokens that have been generated before. 
    Set to > 1 to penalize. Default = 1 (no penalty).
    """

    no_repeat_ngram_size: int = 0
    """Prevent repetitions of ngrams of this size. Default = 0 (turned off)."""

    streaming: bool = False
    """Whether to stream the output. Default = False."""

    @property
    def _default_params(self) -> Mapping[str, Any]:
        """Get the default parameters for calling Titan Takeoff Server."""
        params = {
            "generate_max_length": self.generate_max_length,
            "sampling_topk": self.sampling_topk,
            "sampling_topp": self.sampling_topp,
            "sampling_temperature": self.sampling_temperature,
            "repetition_penalty": self.repetition_penalty,
            "no_repeat_ngram_size": self.no_repeat_ngram_size,
        }
        return params

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "titan_takeoff"

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """Call out to Titan Takeoff generate endpoint.

        Args:
            prompt: The prompt to pass into the model.
            stop: Optional list of stop words to use when generating.

        Returns:
            The string generated by the model.

        Example:
            .. code-block:: python

                prompt = "What is the capital of the United Kingdom?"
                response = model(prompt)

        """
        try:
            if self.streaming:
                text_output = ""
                for chunk in self._stream(
                    prompt=prompt,
                    stop=stop,
                    run_manager=run_manager,
                ):
                    text_output += chunk.text
                return text_output

            url = f"{self.base_url}/generate"
            params = {"text": prompt, **self._default_params}

            response = requests.post(url, json=params)
            response.raise_for_status()
            response.encoding = "utf-8"
            text = ""

            if "message" in response.json():
                text = response.json()["message"]
            else:
                raise ValueError("Something went wrong.")
            if stop is not None:
                text = enforce_stop_tokens(text, stop)
            return text
        except ConnectionError:
            raise ConnectionError(
                "Could not connect to Titan Takeoff server. \
                Please make sure that the server is running."
            )

    def _stream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        """Call out to Titan Takeoff stream endpoint.

        Args:
            prompt: The prompt to pass into the model.
            stop: Optional list of stop words to use when generating.

        Returns:
            The string generated by the model.

        Yields:
            A dictionary like object containing a string token.

        Example:
            .. code-block:: python

                prompt = "What is the capital of the United Kingdom?"
                response = model(prompt)

        """
        url = f"{self.base_url}/generate_stream"
        params = {"text": prompt, **self._default_params}

        response = requests.post(url, json=params, stream=True)
        response.encoding = "utf-8"
        for text in response.iter_content(chunk_size=1, decode_unicode=True):
            if text:
                chunk = GenerationChunk(text=text)
                yield chunk
                if run_manager:
                    run_manager.on_llm_new_token(token=chunk.text)

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {"base_url": self.base_url, **{}, **self._default_params}
