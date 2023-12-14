from __future__ import annotations

import json
import queue
import random
import time
from functools import partial
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

import google.protobuf.json_format
import numpy as np

# Add the import ignore since we are missing type-stubs for these
import tritonclient.grpc as grpcclient  # type: ignore[import]
import tritonclient.http as httpclient  # type: ignore[import]
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseLLM
from langchain_core.outputs import Generation, GenerationChunk, LLMResult
from langchain_core.pydantic_v1 import Field, root_validator
from tritonclient.grpc.service_pb2 import ModelInferResponse  # type: ignore[import]
from tritonclient.utils import np_to_triton_dtype  # type: ignore[import]


class TritonTensorRTError(Exception):
    """Base exception for TritonTensorRT."""


class TritonTensorRTRuntimeError(TritonTensorRTError, RuntimeError):
    """Runtime error for TritonTensorRT."""


class TritonTensorRTLLM(BaseLLM):
    """TRTLLM triton models.

    Arguments:
        server_url: (str) The URL of the Triton inference server to use.
        model_name: (str) The name of the Triton TRT model to use.
        temperature: (str) Temperature to use for sampling
        top_p: (float) The top-p value to use for sampling
        top_k: (float) The top k values use for sampling
        beam_width: (int) Last n number of tokens to penalize
        repetition_penalty: (int) Last n number of tokens to penalize
        length_penalty: (float) The penalty to apply repeated tokens
        tokens: (int) The maximum number of tokens to generate.
        client: The client object used to communicate with the inference server

    Example:
        .. code-block:: python

            from langchain_nvidia_trt import TritonTensorRTLLM

            model = TritonTensorRTLLM()


    """

    server_url: Optional[str] = Field(None, alias="server_url")
    model_name: str = Field(
        ..., description="The name of the model to use, such as 'ensemble'."
    )
    ## Optional args for the model
    temperature: float = 1.0
    top_p: float = 0
    top_k: int = 1
    tokens: int = 100
    beam_width: int = 1
    repetition_penalty: float = 1.0
    length_penalty: float = 1.0
    client: grpcclient.InferenceServerClient
    stop: List[str] = Field(
        default_factory=lambda: ["</s>"], description="Stop tokens."
    )
    seed: int = Field(42, description="The seed to use for random generation.")

    @root_validator(pre=True)
    def validate_environment(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate that python package exists in environment."""
        if not values.get("client"):
            values["client"] = grpcclient.InferenceServerClient(values["server_url"])
        return values

    @property
    def _llm_type(self) -> str:
        """Return type of LLM."""
        return "nvidia-trt-llm"

    @property
    def _model_default_parameters(self) -> Dict[str, Any]:
        return {
            "tokens": self.tokens,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "repetition_penalty": self.repetition_penalty,
            "length_penalty": self.length_penalty,
            "beam_width": self.beam_width,
        }

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get all the identifying parameters."""
        return {
            "server_url": self.server_url,
            "model_name": self.model_name,
            **self._model_default_parameters,
        }

    def _get_invocation_params(self, **kwargs: Any) -> Dict[str, Any]:
        return {**self._model_default_parameters, **kwargs}

    def get_model_list(self) -> List[str]:
        """Get a list of models loaded in the triton server."""
        res = self.client.get_model_repository_index(as_json=True)
        return [model["name"] for model in res["models"]]

    def _get_model_concurrency(self, model_name: str, timeout: int = 1000) -> int:
        """Get the model concurrency."""
        # (WFH): This isn't used anywhere...
        self._load_model(model_name, timeout)
        instances = self.client.get_model_config(model_name, as_json=True)["config"][
            "instance_group"
        ]
        return sum(instance["count"] * len(instance["gpus"]) for instance in instances)

    def _load_model(self, model_name: str, timeout: int = 1000) -> None:
        """Load a model into the server."""
        if self.client.is_model_ready(model_name):
            return

        self.client.load_model(model_name)
        t0 = time.perf_counter()
        t1 = t0
        while not self.client.is_model_ready(model_name) and t1 - t0 < timeout:
            t1 = time.perf_counter()

        if not self.client.is_model_ready(model_name):
            raise TritonTensorRTRuntimeError(
                f"Failed to load {model_name} on Triton in {timeout}s"
            )

    def _generate(
        self,
        prompts: List[str],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> LLMResult:
        invocation_params = self._get_invocation_params(**kwargs)
        # (WFH) TODO: It looks like we were doing this already but...?
        self._load_model(self.model_name)
        # stop_words = stop if stop is not None else self.stop
        generations = []
        # TODO: We should handle the native batching instead.
        for prompt in prompts:
            invoc_params = {**invocation_params, "prompt": [[prompt]]}
            # request_id = str(random.randint(1, 9999999))  # nosec
            # TODO: Fix request ID and stop_word specification
            # TODO: Verify this is actually a string result
            result: str = self._request(
                self.model_name,
                **invoc_params,
            )
            generations.append([Generation(text=result, generation_info={})])
        return LLMResult(generations=generations)

    def _stream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        invocation_params = self._get_invocation_params(**kwargs, prompt=[[prompt]])
        self._load_model(self.model_name)
        stop_words = stop if stop is not None else self.stop

        request_id = str(random.randint(1, 9999999))  # nosec
        result_queue = StreamingResponseGenerator(
            self,
            request_id,
            force_batch=False,
            stop_words=stop_words,
        )
        inputs = self._generate_inputs(stream=True, **invocation_params)
        outputs = self._generate_outputs()
        self.client.start_stream(
            callback=partial(
                self._stream_callback,
                result_queue,
                force_batch=False,
                stop_words=stop_words,
            )
        )
        self.client.async_stream_infer(
            model_name=self.model_name,
            inputs=inputs,
            outputs=outputs,
            request_id=request_id,
        )

        for token in result_queue:
            yield GenerationChunk(text=token)
            if run_manager:
                run_manager.on_llm_new_token(token)

    ##### BELOW ARE METHOD SPREVIOUSLY ONLY THE GRPC CLIENT

    def _request(
        self,
        model_name: str,
        **params: Any,
    ) -> str:
        """Request inferencing from the triton server."""
        if not self.client.is_model_ready(model_name):
            raise RuntimeError("Cannot request streaming, model is not loaded")

        # create model inputs and outputs
        inputs = self._generate_inputs(stream=False, **params)
        outputs = self._generate_outputs()

        # call the model for inference
        result = self.client.infer(model_name, inputs=inputs, outputs=outputs)
        result_str = "".join(
            [val.decode("utf-8") for val in result.as_numpy("text_output").tolist()]
        )
        return self._trim_batch_response(result_str)

    def _generate_outputs(
        self,
    ) -> List[Union[grpcclient.InferRequestedOutput, httpclient.InferRequestedOutput]]:
        """Generate the expected output structure."""
        return [grpcclient.InferRequestedOutput("text_output")]

    def _prepare_tensor(
        self, name: str, input_data: np.ndarray
    ) -> Union[grpcclient.InferInput, httpclient.InferInput]:
        """Prepare an input data structure."""

        t = grpcclient.InferInput(
            name, input_data.shape, np_to_triton_dtype(input_data.dtype)
        )
        t.set_data_from_numpy(input_data)
        return t

    def _generate_inputs(
        self,
        prompt: str,
        tokens: int = 300,
        temperature: float = 1.0,
        top_k: float = 1,
        top_p: float = 0,
        beam_width: int = 1,
        repetition_penalty: float = 1,
        length_penalty: float = 1.0,
        stream: bool = True,
    ) -> List[Union[grpcclient.InferInput, httpclient.InferInput]]:
        """Create the input for the triton inference server."""
        query = np.array(prompt).astype(object)
        request_output_len = np.array([tokens]).astype(np.uint32).reshape((1, -1))
        runtime_top_k = np.array([top_k]).astype(np.uint32).reshape((1, -1))
        runtime_top_p = np.array([top_p]).astype(np.float32).reshape((1, -1))
        temperature_array = np.array([temperature]).astype(np.float32).reshape((1, -1))
        len_penalty = np.array([length_penalty]).astype(np.float32).reshape((1, -1))
        repetition_penalty_array = (
            np.array([repetition_penalty]).astype(np.float32).reshape((1, -1))
        )
        random_seed = np.array([self.seed]).astype(np.uint64).reshape((1, -1))
        beam_width_array = np.array([beam_width]).astype(np.uint32).reshape((1, -1))
        streaming_data = np.array([[stream]], dtype=bool)

        inputs = [
            self._prepare_tensor("text_input", query),
            self._prepare_tensor("max_tokens", request_output_len),
            self._prepare_tensor("top_k", runtime_top_k),
            self._prepare_tensor("top_p", runtime_top_p),
            self._prepare_tensor("temperature", temperature_array),
            self._prepare_tensor("length_penalty", len_penalty),
            self._prepare_tensor("repetition_penalty", repetition_penalty_array),
            self._prepare_tensor("random_seed", random_seed),
            self._prepare_tensor("beam_width", beam_width_array),
            self._prepare_tensor("stream", streaming_data),
        ]
        return inputs

    def _trim_batch_response(self, result_str: str) -> str:
        """Trim batch response by removing prompt and extra generated text."""
        # extract the generated part of the prompt
        # TODO: This assumes llama-style prompting...
        split = result_str.split("[/INST]", 1)
        generated = split[-1]
        end_token = generated.find("</s>")
        if end_token == -1:
            return generated
        generated = generated[:end_token].strip()
        return generated

    def _send_stop_signals(self, model_name: str, request_id: str) -> None:
        """Send the stop signal to the Triton Inference server."""
        stop_inputs = self._generate_stop_signals()
        self.client.async_stream_infer(
            model_name,
            stop_inputs,
            request_id=request_id,
            parameters={"Streaming": True},
        )

    def _generate_stop_signals(
        self,
    ) -> List[grpcclient.InferInput]:
        """Generate the signal to stop the stream."""
        inputs = [
            grpcclient.InferInput("input_ids", [1, 1], "INT32"),
            grpcclient.InferInput("input_lengths", [1, 1], "INT32"),
            grpcclient.InferInput("request_output_len", [1, 1], "UINT32"),
            grpcclient.InferInput("stop", [1, 1], "BOOL"),
        ]
        inputs[0].set_data_from_numpy(np.empty([1, 1], dtype=np.int32))
        inputs[1].set_data_from_numpy(np.zeros([1, 1], dtype=np.int32))
        inputs[2].set_data_from_numpy(np.array([[0]], dtype=np.uint32))
        inputs[3].set_data_from_numpy(np.array([[True]], dtype="bool"))
        return inputs

    @staticmethod
    def _process_result(result: Dict[str, str]) -> str:
        """Post-process the result from the server."""

        message = ModelInferResponse()
        google.protobuf.json_format.Parse(json.dumps(result), message)
        infer_result = grpcclient.InferResult(message)
        np_res = infer_result.as_numpy("text_output")

        generated_text = ""
        if np_res is not None:
            generated_text = "".join([token.decode() for token in np_res])

        return generated_text

    def _stream_callback(
        self,
        result_queue: queue.Queue[Union[Optional[Dict[str, str]], str]],
        force_batch: bool,
        result: grpcclient.InferResult,
        error: str,
        stop_words: List[str],
    ) -> None:
        """Add streamed result to queue."""
        if error:
            result_queue.put(error)
        else:
            response_raw: dict = result.get_response(as_json=True)
            # TODO: Check the response is a map rather than a string
            if "outputs" in response_raw:
                # the very last response might have no output, just the final flag
                response = self._process_result(response_raw)
                if force_batch:
                    response = self._trim_batch_response(response)

                if response in stop_words:
                    result_queue.put(None)
                else:
                    result_queue.put(response)

            if response_raw["parameters"]["triton_final_response"]["bool_param"]:
                # end of the generation
                result_queue.put(None)

    def stop_stream(
        self, model_name: str, request_id: str, signal: bool = True
    ) -> None:
        """Close the streaming connection."""
        if signal:
            self._send_stop_signals(model_name, request_id)
        self.client.stop_stream()


class StreamingResponseGenerator(queue.Queue[Optional[str]]):
    """A Generator that provides the inference results from an LLM."""

    def __init__(
        self,
        client: grpcclient.InferenceServerClient,
        request_id: str,
        force_batch: bool,
        stop_words: Sequence[str],
    ) -> None:
        """Instantiate the generator class."""
        super().__init__()
        self.client = client
        self.request_id = request_id
        self._batch = force_batch
        self._stop_words = stop_words

    def __iter__(self) -> StreamingResponseGenerator:
        """Return self as a generator."""
        return self

    def __next__(self) -> str:
        """Return the next retrieved token."""
        val = self.get()
        if val is None or val in self._stop_words:
            self.client.stop_stream(
                "tensorrt_llm", self.request_id, signal=not self._batch
            )
            raise StopIteration()
        return val
