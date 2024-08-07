import asyncio
from typing import Sequence

import requests
import torch

from esm.sdk.api import (
    ESM3InferenceClient,
    ESMProtein,
    ESMProteinError,
    ESMProteinTensor,
    ForwardAndSampleOutput,
    ForwardTrackData,
    GenerationConfig,
    ProteinType,
    SamplingConfig,
    SamplingTrackConfig,
)
from esm.utils.misc import maybe_list, maybe_tensor
from esm.utils.types import FunctionAnnotation


def _list_to_function_annotations(l) -> list[FunctionAnnotation] | None:
    if l is None or len(l) <= 0:
        return None
    return [FunctionAnnotation(*t) for t in l]


class ESM3ForgeInferenceClient(ESM3InferenceClient):
    def __init__(
        self,
        model: str,
        url: str = "https://forge.evolutionaryscale.ai",
        token: str = "",
        request_timeout: int | None = None,
    ):
        if token == "":
            raise RuntimeError(
                "Please provide a token to connect to Forge via token=YOUR_API_TOKEN_HERE"
            )
        self.model = model
        self.url = url
        self.token = token
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.request_timeout = request_timeout

    def generate(self, input: ProteinType, config: GenerationConfig) -> ProteinType:
        if isinstance(input, ESMProtein):
            output = self.__generate_protein(input, config)
        elif isinstance(input, ESMProteinTensor):
            output = self.__generate_protein_tensor(input, config)
        else:
            return ESMProteinError(error_msg=f"Unknown input type {type(input)}")

        if (
            isinstance(output, ESMProtein)
            and isinstance(input, ESMProtein)
            and config.track
            not in [
                "function",
                "residue_annotations",
            ]
        ):
            # Function and residue annotation encoding/decoding is lossy
            # There is no guarantee that decoding encoded tokens will yield the same input
            output.function_annotations = input.function_annotations

        return output

    def batch_generate(
        self, inputs: list[ProteinType], configs: list[GenerationConfig]
    ) -> Sequence[ProteinType]:
        """Forge supports auto-batching. So batch_generate() for the Forge client
        is as simple as running a collection of generate() in parallel using asyncio.
        """
        loop = asyncio.get_event_loop()

        async def _async_generate():
            futures = [
                loop.run_in_executor(None, self.generate, protein, config)
                for protein, config in zip(inputs, configs)
            ]
            return await asyncio.gather(*futures, return_exceptions=True)

        results = loop.run_until_complete(_async_generate())

        return [
            r if not isinstance(r, BaseException) else ESMProteinError(str(r))
            for r in results
        ]

    def __generate_protein(
        self,
        input: ESMProtein,
        config: GenerationConfig,
    ) -> ESMProtein | ESMProteinError:
        req = {}
        req["sequence"] = input.sequence
        req["secondary_structure"] = input.secondary_structure
        req["sasa"] = input.sasa
        if input.function_annotations is not None:
            req["function"] = [x.to_tuple() for x in input.function_annotations]
        req["coordinates"] = maybe_list(input.coordinates, convert_nan_to_none=True)

        request = {
            "model": self.model,
            "inputs": req,
            "track": config.track,
            "invalid_ids": config.invalid_ids,
            "schedule": config.schedule,
            "num_steps": config.num_steps,
            "temperature": config.temperature,
            "top_p": config.top_p,
            "condition_on_coordinates_only": config.condition_on_coordinates_only,
        }

        try:
            data = self.__post("generate", request)
        except RuntimeError as e:
            return ESMProteinError(error_msg=str(e))

        return ESMProtein(
            sequence=data["outputs"]["sequence"],
            secondary_structure=data["outputs"]["secondary_structure"],
            sasa=data["outputs"]["sasa"],
            function_annotations=_list_to_function_annotations(
                data["outputs"]["function"]
            ),
            coordinates=maybe_tensor(
                data["outputs"]["coordinates"], convert_none_to_nan=True
            ),
            plddt=maybe_tensor(data["outputs"]["plddt"]),
            ptm=maybe_tensor(data["outputs"]["ptm"]),
        )

    def __generate_protein_tensor(
        self,
        input: ESMProteinTensor,
        config: GenerationConfig,
    ) -> ESMProteinTensor | ESMProteinError:
        req = {}
        req["sequence"] = maybe_list(input.sequence)
        req["structure"] = maybe_list(input.structure)
        req["secondary_structure"] = maybe_list(input.secondary_structure)
        req["sasa"] = maybe_list(input.sasa)
        req["function"] = maybe_list(input.function)
        req["coordinates"] = maybe_list(input.coordinates, convert_nan_to_none=True)
        req["residue_annotation"] = maybe_list(input.residue_annotations)

        request = {
            "model": self.model,
            "inputs": req,
            "track": config.track,
            "invalid_ids": config.invalid_ids,
            "schedule": config.schedule,
            "num_steps": config.num_steps,
            "temperature": config.temperature,
            "top_p": config.top_p,
            "condition_on_coordinates_only": config.condition_on_coordinates_only,
        }

        try:
            data = self.__post("generate_tensor", request)
        except RuntimeError as e:
            return ESMProteinError(error_msg=str(e))

        def _field_to_tensor(field, convert_none_to_nan: bool = False):
            if field not in data["outputs"]:
                return None
            return maybe_tensor(
                data["outputs"][field], convert_none_to_nan=convert_none_to_nan
            )

        output = ESMProteinTensor(
            sequence=_field_to_tensor("sequence"),
            structure=_field_to_tensor("structure"),
            secondary_structure=_field_to_tensor("secondary_structure"),
            sasa=_field_to_tensor("sasa"),
            function=_field_to_tensor("function"),
            residue_annotations=_field_to_tensor("residue_annotation"),
            coordinates=_field_to_tensor("coordinates", convert_none_to_nan=True),
        )

        return output

    def forward_and_sample(
        self, input: ESMProteinTensor, sampling_configuration: SamplingConfig
    ) -> ForwardAndSampleOutput:
        req = {}
        sampling_config = {}
        embedding_config = None  # TODO(zeming)
        if (
            sampling_configuration.return_mean_embedding
            or sampling_configuration.return_per_residue_embeddings
        ):
            print(
                "Warning: return_mean_embedding and return_per_residue_embeddings are not supported by Forge."
            )

        req["sequence"] = maybe_list(input.sequence)
        req["structure"] = maybe_list(input.structure)
        req["secondary_structure"] = maybe_list(input.secondary_structure)
        req["sasa"] = maybe_list(input.sasa)
        req["function"] = maybe_list(input.function)
        req["coordinates"] = maybe_list(input.coordinates, convert_nan_to_none=True)
        req["residue_annotation"] = maybe_list(input.residue_annotations)

        def do_track(t: str):
            track: SamplingTrackConfig | None
            if (track := getattr(sampling_configuration, t, None)) is None:
                sampling_config[t] = None
            else:
                sampling_config[t] = {
                    "temperature": track.temperature,
                    "top_p": track.top_p,
                    "only_sample_masked_tokens": track.only_sample_masked_tokens,
                    "invalid_ids": track.invalid_ids,
                    "topk_logprobs": track.topk_logprobs,
                }

        do_track("sequence")
        do_track("structure")
        do_track("secondary_structure")
        do_track("sasa")
        do_track("function")

        request = {
            "model": self.model,
            "inputs": req,
            "sampling_config": sampling_config,
            "embedding_config": embedding_config,
        }
        data = self.__post("forward_and_sample", request)

        def get(k, field):
            if data[k] is None:
                return None
            v = data[k][field]
            return torch.tensor(v) if v is not None else None

        tokens = ESMProteinTensor(
            sequence=get("sequence", "tokens"),
            structure=get("structure", "tokens"),
            secondary_structure=get("secondary_structure", "tokens"),
            sasa=get("sasa", "tokens"),
            function=get("function", "tokens"),
        )

        def get_track(field):
            return ForwardTrackData(
                sequence=get("sequence", field),
                structure=get("structure", field),
                secondary_structure=get("secondary_structure", field),
                sasa=get("sasa", field),
                function=get("function", field),
            )

        def operate_on_track(track: ForwardTrackData, fn):
            apply = lambda x: fn(x) if x is not None else None
            return ForwardTrackData(
                sequence=apply(track.sequence),
                structure=apply(track.structure),
                secondary_structure=apply(track.secondary_structure),
                sasa=apply(track.sasa),
                function=apply(track.function),
            )

        logprob = get_track("logprobs")
        output = ForwardAndSampleOutput(
            protein_tensor=tokens,
            logprob=logprob,
            prob=operate_on_track(logprob, torch.exp),
            entropy=get_track("entropy"),
            topk_logprob=get_track("topk_logprobs"),
            topk_tokens=get_track("topk_tokens"),
        )
        return output

    def encode(self, input: ESMProtein) -> ESMProteinTensor:
        tracks = {}
        tracks["sequence"] = input.sequence
        tracks["secondary_structure"] = input.secondary_structure
        tracks["sasa"] = input.sasa
        if input.function_annotations is not None:
            tracks["function"] = [x.to_tuple() for x in input.function_annotations]
        tracks["coordinates"] = maybe_list(input.coordinates, convert_nan_to_none=True)

        request = {"inputs": tracks, "model": self.model}

        data = self.__post("encode", request)

        return ESMProteinTensor(
            sequence=maybe_tensor(data["outputs"]["sequence"]),
            structure=maybe_tensor(data["outputs"]["structure"]),
            coordinates=maybe_tensor(
                data["outputs"]["coordinates"], convert_none_to_nan=True
            ),
            secondary_structure=maybe_tensor(data["outputs"]["secondary_structure"]),
            sasa=maybe_tensor(data["outputs"]["sasa"]),
            function=maybe_tensor(data["outputs"]["function"]),
            residue_annotations=maybe_tensor(data["outputs"]["residue_annotation"]),
        )

    def decode(
        self,
        input: ESMProteinTensor,
    ) -> ESMProtein:
        tokens = {}
        tokens["sequence"] = maybe_list(input.sequence)
        tokens["structure"] = maybe_list(input.structure)
        tokens["secondary_structure"] = maybe_list(input.secondary_structure)
        tokens["sasa"] = maybe_list(input.sasa)
        tokens["function"] = maybe_list(input.function)
        tokens["residue_annotation"] = maybe_list(input.residue_annotations)
        tokens["coordinates"] = maybe_list(input.coordinates, convert_nan_to_none=True)

        request = {
            "model": self.model,
            "inputs": tokens,
        }

        data = self.__post("decode", request)

        return ESMProtein(
            sequence=data["outputs"]["sequence"],
            secondary_structure=data["outputs"]["secondary_structure"],
            sasa=data["outputs"]["sasa"],
            function_annotations=_list_to_function_annotations(
                data["outputs"]["function"]
            ),
            coordinates=maybe_tensor(
                data["outputs"]["coordinates"], convert_none_to_nan=True
            ),
            plddt=maybe_tensor(data["outputs"]["plddt"]),
            ptm=maybe_tensor(data["outputs"]["ptm"]),
        )

    def __post(self, endpoint, request):
        response = requests.post(
            f"{self.url}/api/v1/{endpoint}",
            json=request,
            headers=self.headers,
            timeout=self.request_timeout,
        )

        if not response.ok:
            raise RuntimeError(f"Failure in {endpoint}: {response.text}")

        data = response.json()
        # Nextjs puts outputs dict under "data" key.
        # Lift it up for easier downstream processing.
        if "outputs" not in data and "data" in data:
            data = data["data"]

        return data
