import json
import types
import warnings
from pathlib import Path
from typing import Dict, Optional, Tuple, cast

import arviz as az
import pandas as pd
import pymc as pm
from pymc import Model, str_for_dist
from pymc.backends import NDArray
from pymc.backends.base import MultiTrace
from pytensor.tensor import TensorVariable
from xarray import Dataset

from pymc_marketing.model_builder import ModelBuilder


class CLVModel(ModelBuilder):
    _model_type = ""

    def __init__(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        model_config: Optional[Dict] = None,
        sampler_config: Optional[Dict] = None,
    ):
        super().__init__(model_config, sampler_config)
        self.data = data

    def __repr__(self):
        return f"{self._model_type}\n{self.model.str_repr()}"

    def _add_fit_data_group(self, data: pd.DataFrame) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=UserWarning,
                message="The group fit_data is not defined in the InferenceData scheme",
            )
            assert self.idata is not None
            self.idata.add_groups(fit_data=data.to_xarray())

    def fit(  # type: ignore
        self,
        fit_method: str = "mcmc",
        **kwargs,
    ) -> az.InferenceData:
        """Infer model posterior

        Parameters
        ----------
        fit_method: str
            Method used to fit the model. Options are:
            - "mcmc": Samples from the posterior via `pymc.sample` (default)
            - "map": Finds maximum a posteriori via `pymc.find_MAP`
        kwargs:
            Other keyword arguments passed to the underlying PyMC routines
        """

        self.build_model()  # type: ignore

        if fit_method == "mcmc":
            idata = self._fit_mcmc(**kwargs)
        elif fit_method == "map":
            idata = self._fit_MAP(**kwargs)
        else:
            raise ValueError(
                f"Fit method options are ['mcmc', 'map'], got: {fit_method}"
            )

        self.idata = idata
        self.set_idata_attrs(self.idata)
        if self.data is not None:
            self._add_fit_data_group(self.data)

        return self.idata

    def _fit_mcmc(self, **kwargs) -> az.InferenceData:
        """
        Fit a model using the data passed as a parameter.
        Sets attrs to inference data of the model.


        Parameters
        ----------
        X : array-like if sklearn is available, otherwise array, shape (n_obs, n_features)
            The training input samples.
        y : array-like if sklearn is available, otherwise array, shape (n_obs,)
            The target values (real numbers).
        **kwargs : Any
            Custom sampler settings can be provided in form of keyword arguments.

        Returns
        -------
        self : az.InferenceData
            returns inference data of the fitted model.
        """
        sampler_config = {}
        if self.sampler_config is not None:
            sampler_config = self.sampler_config.copy()
        sampler_config.update(**kwargs)
        return pm.sample(**sampler_config, model=self.model)

    def _fit_MAP(self, **kwargs) -> az.InferenceData:
        """Find model maximum a posteriori using scipy optimizer"""
        model = self.model
        map_res = pm.find_MAP(model=model, **kwargs)
        # Filter non-value variables
        value_vars_names = set(v.name for v in cast(Model, model).value_vars)
        map_res = {k: v for k, v in map_res.items() if k in value_vars_names}
        # Convert map result to InferenceData
        map_strace = NDArray(model=model)
        map_strace.setup(draws=1, chain=0)
        map_strace.record(map_res)
        map_strace.close()
        trace = MultiTrace([map_strace])
        return pm.to_inference_data(trace, model=model)

    @classmethod
    def load(cls, fname: str):
        """
        Creates a ModelBuilder instance from a file,
        Loads inference data for the model.

        Parameters
        ----------
        fname : string
            This denotes the name with path from where idata should be loaded from.

        Returns
        -------
        Returns an instance of ModelBuilder.

        Raises
        ------
        ValueError
            If the inference data that is loaded doesn't match with the model.
        Examples
        --------
        >>> class MyModel(ModelBuilder):
        >>>     ...
        >>> name = './mymodel.nc'
        >>> imported_model = MyModel.load(name)
        """
        filepath = Path(str(fname))
        idata = az.from_netcdf(filepath)
        return cls._build_with_idata(idata)

    @classmethod
    def _build_with_idata(cls, idata: az.InferenceData):
        dataset = idata.fit_data.to_dataframe()
        model = cls(
            dataset,
            model_config=json.loads(idata.attrs["model_config"]),  # type: ignore
            sampler_config=json.loads(idata.attrs["sampler_config"]),
        )
        model.idata = idata
        model.build_model()  # type: ignore
        if model.id != idata.attrs["id"]:
            raise ValueError(f"Inference data not compatible with {cls._model_type}")
        return model

    def thin_fit_result(self, keep_every: int):
        """Return a copy of the model with a thinned fit result.

        This is useful when computing summary statistics that may require too much memory per posterior draw.

        Examples
        --------

        .. code-block:: python

            fitted_gg = ...
            fitted bg = ...

            fitted_gg_thinned = fitted_gg.thin_fit_result(keep_every=10)
            fitted_bg_thinned = fitted_bg.thin_fit_result(keep_every=10)

            clv_thinned = fitted_gg_thinned.expected_customer_lifetime_value(
                transaction_model=fitted_bg_thinned,
                customer_id=t.index,
                frequency=t["frequency"],
                recency=t["recency"],
                T=t["T"],
                mean_transaction_value=t["monetary_value"],
            )

        """
        self.fit_result  # Raise Error if fit didn't happen yet
        assert self.idata is not None
        new_idata = self.idata.isel(draw=slice(None, None, keep_every)).copy()
        return type(self)._build_with_idata(new_idata)

    @staticmethod
    def _check_prior_ndim(prior, ndim: int = 0):
        if prior.ndim != ndim:
            raise ValueError(
                f"Prior variable {prior} must be have {ndim} ndims, but it has {prior.ndim} ndims."
            )

    @staticmethod
    def _create_distribution(dist: Dict, ndim: int = 0) -> TensorVariable:
        try:
            prior_distribution = getattr(pm, dist["dist"]).dist(**dist["kwargs"])
            CLVModel._check_prior_ndim(prior_distribution, ndim)
        except AttributeError:
            raise ValueError(f"Distribution {dist['dist']} does not exist in PyMC")
        return prior_distribution

    @staticmethod
    def _process_priors(
        *priors: TensorVariable, check_ndim: bool = True
    ) -> Tuple[TensorVariable, ...]:
        """Check that each prior variable is unique and attach `str_repr` method."""
        if len(priors) != len(set(priors)):
            raise ValueError("Prior variables must be unique")
        # Related to https://github.com/pymc-devs/pymc/issues/6311
        for prior in priors:
            prior.str_repr = types.MethodType(str_for_dist, prior)  # type: ignore
        return priors

    @property
    def default_sampler_config(self) -> Dict:
        return {}

    @property
    def _serializable_model_config(self) -> Dict:
        return self.model_config

    @property
    def fit_result(self) -> Dataset:
        if self.idata is None or "posterior" not in self.idata:
            raise RuntimeError("The model hasn't been fit yet, call .fit() first")
        return self.idata["posterior"]

    @fit_result.setter
    def fit_result(self, res: az.InferenceData) -> None:
        if self.idata is None:
            self.idata = res
        elif "posterior" in self.idata:
            warnings.warn("Overriding pre-existing fit_result")
            self.idata.posterior = res
        else:
            self.idata.posterior = res

    def fit_summary(self, **kwargs):
        res = self.fit_result
        # Map fitting only gives one value, so we return it. We use arviz
        # just to get it nicely into a DataFrame
        if res.chain.size == 1 and res.draw.size == 1:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = az.summary(self.fit_result, **kwargs, kind="stats")
            return res["mean"].rename("value")
        else:
            return az.summary(self.fit_result, **kwargs)

    @property
    def output_var(self):
        pass

    def _generate_and_preprocess_model_data(self, *args, **kwargs):
        pass

    def _data_setter(self):
        pass
