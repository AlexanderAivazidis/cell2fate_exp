from typing import List, Optional
from datetime import date
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from anndata import AnnData
from pyro import clear_param_store
from scvi import REGISTRY_KEYS
from scvi.data import AnnDataManager
from scvi.data.fields import (
    CategoricalObsField,
    LayerField,
    NumericalJointObsField,
    NumericalObsField,
)
from scvi.model.base import BaseModelClass, PyroSampleMixin, PyroSviTrainMixin
from scvi.utils import setup_anndata_dsp
import pyro.distributions as dist
import torch
import scanpy as sc
import contextlib
import io
from numpy.linalg import norm
from scipy.sparse import csr_matrix
from numpy import inner
import scvelo as scv
import scipy
import gseapy as gp
from cell2fate._pyro_base_cell2fate_module import Cell2FateBaseModule
from cell2fate._pyro_mixin import PltExportMixin, QuantileMixin
from ._cell2fate_DynamicalModel_SequentialModules_module import \
Cell2fate_DynamicalModel_SequentialModules_module
from cell2fate.utils import multiplot_from_generator

from cell2fate.utils import mu_mRNA_continousAlpha_globalTime_twoStates
import cell2fate as c2f

class Cell2fate_DynamicalModel_SequentialModules(QuantileMixin, PyroSampleMixin, PyroSviTrainMixin, PltExportMixin, BaseModelClass):
    """
    Cell2fate model. User-end model class. See Module class for description of the model.

    Parameters
    ----------
    adata
        single-cell AnnData object that has been registered via :func:`~scvi.data.setup_anndata`
        and contains spliced and unspliced counts in adata.layers['spliced'], adata.layers['unspliced']
    **model_kwargs
        Keyword args for :class:`~scvi.external.LocationModelLinearDependentWMultiExperimentModel`

    Examples
    --------
    TODO add example
    >>>
    """

    def __init__(
        self,
        adata: AnnData,
        model_class=None,
        **model_kwargs,
    ):
        # in case any other model was created before that shares the same parameter names.
        clear_param_store()

        super().__init__(adata)

        if model_class is None:
            model_class = Cell2fate_DynamicalModel_SequentialModules_module

        self.module = Cell2FateBaseModule(
            model=model_class,
            n_obs=self.summary_stats["n_cells"],
            n_vars=self.summary_stats["n_vars"],
            n_batch=self.summary_stats["n_batch"],
            **model_kwargs,
        )
        self._model_summary_string = f'Cell2fate Dynamical Model with the following params: \nn_batch: {self.summary_stats["n_batch"]} '
        self.init_params_ = self._get_init_params(locals())

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        layer: Optional[str] = None,
        batch_key: Optional[str] = None,
        labels_key: Optional[str] = None,
        unspliced_label = 'unspliced',
        spliced_label = 'spliced',
        cluster_label = None,
        **kwargs,
    ):
        """
        %(summary)s.

        Parameters
        ----------
        %(param_layer)s
        %(param_batch_key)s
        %(param_labels_key)s
        """
        setup_method_args = cls._get_setup_method_args(**locals())
        adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
        if cluster_label:
            anndata_fields = [
                LayerField('unspliced', unspliced_label, is_count_data=True),
                LayerField('spliced', spliced_label, is_count_data=True),
                CategoricalObsField('clusters', cluster_label),
                NumericalObsField(REGISTRY_KEYS.INDICES_KEY, "_indices"),
                CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key)
            ]
        else:
            anndata_fields = [
                LayerField('unspliced', unspliced_label, is_count_data=True),
                LayerField('spliced', spliced_label, is_count_data=True),
                CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key),
                NumericalObsField(REGISTRY_KEYS.INDICES_KEY, "_indices"),
            ]
        adata_manager = AnnDataManager(fields=anndata_fields, setup_method_args=setup_method_args)
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)

    def train(
        self,
        max_epochs: int = 500,
        batch_size: int = 1000,
        train_size: float = 1,
        lr: float = 0.01,
        **kwargs,
    ):
        """Train the model with useful defaults

        Parameters
        ----------
        max_epochs
            Number of passes through the dataset. If `None`, defaults to
            `np.min([round((20000 / n_cells) * 400), 400])`
        train_size
            Size of training set in the range [0.0, 1.0].
        batch_size
            Minibatch size to use during training. If `None`, no minibatching occurs and all
            data is copied to device (e.g., GPU).
        lr
            Optimiser learning rate (default optimiser is :class:`~pyro.optim.ClippedAdam`).
            Specifying optimiser via plan_kwargs overrides this choice of lr.
        kwargs
            Other arguments to scvi.model.base.PyroSviTrainMixin().train() method
        """
        
        self.max_epochs = max_epochs
        kwargs["max_epochs"] = max_epochs
        kwargs["batch_size"] = batch_size
        kwargs["train_size"] = train_size
        kwargs["lr"] = lr

        super().train(**kwargs)
        
    def _export2adata(self, samples):
        r"""
        Export key model variables and samples

        Parameters
        ----------
        samples
            dictionary with posterior mean, 5%/95% quantiles, SD, samples, generated by ``.sample_posterior()``

        Returns
        -------
            Updated dictionary with additional details is saved to ``adata.uns['mod']``.
        """
        # add factor filter and samples of all parameters to unstructured data
        results = {
            "model_name": str(self.module.__class__.__name__),
            "date": str(date.today()),
            "var_names": self.adata.var_names.tolist(),
            "obs_names": self.adata.obs_names.tolist(),
            "post_sample_means": samples["post_sample_means"],
            "post_sample_stds": samples["post_sample_stds"],
            "post_sample_q05": samples["post_sample_q05"],
            "post_sample_q95": samples["post_sample_q95"],
        }

        return results
    
    def compute_module_summary_statistics(self, adata):
        '''Computes the contribution of each module to mRNA molecules in each cell'''
        if scipy.sparse.issparse(self.adata_manager.get_from_registry('unspliced')):
            observed_total = torch.sum(torch.sum(torch.stack([torch.tensor(self.adata_manager.get_from_registry('unspliced').toarray()),
                          torch.tensor(self.adata_manager.get_from_registry('spliced').toarray())], axis = -1), axis = -1), axis = -1)
        else:
            observed_total = torch.sum(torch.sum(torch.stack([torch.tensor(self.adata_manager.get_from_registry('unspliced')),
                          torch.tensor(self.adata_manager.get_from_registry('spliced'))], axis = -1), axis = -1), axis = -1)
        inferred_total = torch.sum(torch.sum(torch.tensor(self.samples['post_sample_means']['mu_expression']), axis = -1), axis = -1)
        for m in range(self.module.model.n_modules):
            mu_m = mu_mRNA_continousAlpha_globalTime_twoStates(
                torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:]),
                torch.tensor(0.),
                torch.tensor(self.samples['post_sample_means']['beta_g']),
                torch.tensor(self.samples['post_sample_means']['gamma_g']),
                torch.tensor(self.samples['post_sample_means']['lam_mi'][m,:]),
                torch.tensor(self.samples['post_sample_means']['T_c'][:,:,0]),
                torch.tensor(self.samples['post_sample_means']['T_mON'][:,:,m]),
                torch.tensor(self.samples['post_sample_means']['T_mOFF'][:,:,m]),
                torch.zeros((self.module.model.n_obs, self.module.model.n_vars)))
            adata.obs['Module ' + str(m) + ' Weight'] = torch.clip(torch.sum(torch.sum(mu_m, axis = -1), axis = -1)/inferred_total,
                                                                   min = 0.0, max = 1.0)
            ss_total = torch.sum(torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:])/torch.tensor(self.samples['post_sample_means']['gamma_g']) + \
        torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:])/torch.tensor(self.samples['post_sample_means']['beta_g']), axis = 1)
            adata.obs['Module ' + str(m) + ' Activation'] = torch.sum(torch.sum(mu_m, axis = -1), axis = -1)/ss_total
            adata.obs['Module ' + str(m) + ' State'] = 'OFF'
            adata.obs['Module ' + str(m) + ' State'
             ][self.samples['post_sample_means']['T_c'][:,0,0] > self.samples['post_sample_means']['T_mON'][0,0,m]
              ] = 'Induction'
            adata.obs['Module ' + str(m) + ' State'
             ][self.samples['post_sample_means']['T_c'][:,0,0] > self.samples['post_sample_means']['T_mOFF'][0,0,m]
              ] = 'Repression'
            adata.obs['Module ' + str(m) + ' State'
             ][adata.obs['Module ' + str(m) + ' Activation'] > 0.95
              ] = 'ON'
            adata.obs['Module ' + str(m) + ' State'
             ][adata.obs['Module ' + str(m) + ' Activation'] < 0.05
              ] = 'OFF'
        return adata

    def plot_module_summary_statistics(self, adata, save = None):
        'Weight, Activation, Velocity, Switch ON/OFF time (histogram)'
        fig, ax = plt.subplots(self.module.model.n_modules, 3, figsize = (15, 4*self.module.model.n_modules))
        for i in range(self.module.model.n_modules):
            sc.pl.umap(adata, color = ['Module ' + str(i) + ' Weight'], legend_loc = None,
                        size = 200, color_map = 'viridis', ax = ax[i,0], show = False)
            sc.pl.umap(adata, color = ['Module ' + str(i) + ' Activation'], legend_loc = None,
                        size = 200, color_map = 'viridis', ax = ax[i,1], show = False)
            sc.pl.umap(adata, color = ['Module ' + str(i) + ' State'], legend_loc = 'on data',
                        size = 200, ax = ax[i,2], show = False,
                       palette =  {'ON': 'lime', 'OFF': 'grey', 'Induction': 'lightgreen', 'Repression': 'orange'})
            plt.tight_layout()
        if save:
            plt.savefig(save)

    def export_posterior(
        self,
        adata,
        sample_kwargs = {"num_samples": 30, "batch_size" : None,
                         "use_gpu" : True, 'return_samples': True},
        export_slot: str = "mod",
        full_velocity_posterior = False,
        normalize = True):
        """
        Summarises posterior distribution and exports results to anndata object. 
        Also computes RNAvelocity (based on posterior of rates)
        and normalized counts (based on posterior of technical variables)
        1. adata.obs: Latent time, sequencing depth constant
        2. adata.var: transcription/splicing/degredation rates, switch on and off times
        3. adata.uns: Posterior of all parameters ('mean', 'sd', 'q05', 'q95' and optionally all samples),
        model name, date
        4. adata.layers: 'velocity' (expected gradient of spliced counts), 'velocity_sd' (uncertainty in this gradient),
        'spliced_norm', 'unspliced_norm' (normalized counts)
        5. adata.uns: If "return_samples: True" and "full_velocity_posterior = True" full posterior distribution for velocity
        is saved in "adata.uns['velocity_posterior']". 
        Parameters
        ----------
        adata
            anndata object where results should be saved
        sample_kwargs
            optinoally a dictionary of arguments for self.sample_posterior, namely:
                num_samples - number of samples to use (Default = 1000).
                batch_size - data batch size (keep low enough to fit on GPU, default 2048).
                use_gpu - use gpu for generating samples?
                return_samples - export all posterior samples? (Otherwise just summary statistics)
        export_slot
            adata.uns slot where to export results
        full_velocity_posterior
            whether to save full posterior of velocity (only possible if "return_samples: True")
        normalize
            whether to compute normalized spliced and unspliced counts based on posterior of technical variables
        Returns
        -------
        adata with posterior added in adata.obs, adata.var and adata.uns
        """
        
#         if sample_kwargs['batch_size'] == None:
        sample_kwargs['batch_size'] = adata.n_obs
        print("sample_kwargs['batch_size']", sample_kwargs['batch_size'])
#         sample_kwargs = sample_kwargs if isinstance(sample_kwargs, dict) else dict()

        # generate samples from posterior distributions for all parameters
        # and compute mean, 5%/95% quantiles and standard deviation
        self.samples = self.sample_posterior(**sample_kwargs)

        # export posterior distribution summary for all parameters and
        # annotation (model, date, var, obs and cell type names) to anndata object
        adata.uns[export_slot] = self._export2adata(self.samples)

        if sample_kwargs['return_samples']:
            print('Warning: Saving ALL posterior samples. Specify "return_samples: False" to save just summary statistics.')
            adata.uns[export_slot]['post_samples'] = self.samples['posterior_samples']

        adata.obs['Time (hours)'] = self.samples['post_sample_means']['T_c'].flatten() - np.min(self.samples['post_sample_means']['T_c'].flatten())
        adata.obs['Time Uncertainty (sd)'] = self.samples['post_sample_stds']['T_c'].flatten()
        
#         adata.layers['spliced mean'] = self.samples['post_sample_means']['mu_expression'][...,1]
#         adata.layers['velocity'] = torch.tensor(self.samples['post_sample_means']['beta_g']) * \
#         self.samples['post_sample_means']['mu_expression'][...,0] - \
#         torch.tensor(self.samples['post_sample_means']['gamma_g']) * \
#         self.samples['post_sample_means']['mu_expression'][...,1]

        return adata

    def compute_velocity_graph_Bergen2020(mod, adata, n_neighbours = None, full_posterior = True, spliced_key = 'Ms',
                                          velocity_key = 'velocity'):
        """
        Computes a "velocity graph" similar to the method in:
        "Bergen et al. (2020), Generalizing RNA velocity to transient cell states through dynamical modeling"

        Parameters
        ----------
        adata
            anndata object with velocity information in adata.layers['velocity'] (expectation value) or 
            adata.uns['velocity_posterior'] (full posterior). Also normalized spliced counts in adata.layers['spliced_norm'].
        n_neighbours
            how many nearest neighbours to consider (all non nearest neighbours have edge weights set to 0)
            if not specified, 10% of the total number of cells is used.
        full_posterior
            whether to use full posterior to compute velocity graph (otherwise expectation value is used)  
        Returns
        -------
        velocity_graph
        """
        M = len(adata.obs_names)
        if not n_neighbours:
            n_neighbours = int(np.round(M*0.05, 0))
        scv.pp.neighbors(adata, n_neighbors = n_neighbours)
        adata.obsp['binary'] = adata.obsp['connectivities'] != 0
        distances = []
        velocities = []
        cosines = []
        transition_probabilities = []
        matrices = []
        if full_posterior:
            for i in range(M):
                distances += [adata.layers[spliced_key][adata.obsp['binary'].toarray()[i,:],:] - adata.layers[spliced_key][i,:].flatten()]
                velocities += [adata.uns['velocity_posterior'][:,i,:]]
                cosines += [inner(distances[i], velocities[i])/(norm(distances[i])*norm(velocities[i]))]
                transition_probabilities += [np.exp(2*cosines[i])]
                transition_probabilities[i] = transition_probabilities[i]/np.sum(transition_probabilities[i], axis = 0)
                matrices += [csr_matrix((np.mean(np.array(transition_probabilities[i]), axis = 1),
                        (np.repeat(i, len(transition_probabilities[i])), np.where(adata.obsp['binary'][i,:].toarray())[1])),
                                       shape=(M, M))]
        else:
            for i in range(M):
                distances += [adata.layers[spliced_key][adata.obsp['binary'].toarray()[i,:],:] - adata.layers[spliced_key][i,:].flatten()]
                velocities += [adata.layers[velocity_key][i,:].reshape(1,len(adata.var_names))]
                cosines += [inner(distances[i], velocities[i])/(norm(distances[i])*norm(velocities[i]))]
                transition_probabilities += [np.exp(2*cosines[i])]
                transition_probabilities[i] = transition_probabilities[i]/np.sum(transition_probabilities[i], axis = 0)
                matrices += [csr_matrix((np.mean(np.array(transition_probabilities[i]), axis = 1),
                        (np.repeat(i, len(transition_probabilities[i])), np.where(adata.obsp['binary'][i,:].toarray())[1])),
                                       shape=(M, M))]
        return sum(matrices)

    def compute_and_plot_module_velocity(self, adata, delete = True, plot = True, save = None,
                                     plotting_kwargs = {"color": 'clusters', 'legend_fontsize': 10,
                                                        'legend_loc': 'right_margin', 'min_mass': 4}):
        """
        Computes the RNA velocity produced by each module, as well as associated "velocity graph" and 
        then plots results on a UMAP based on the method in:
        "Bergen et al. (2020), Generalizing RNA velocity to transient cell states through dynamical modeling"
        """

        n_modules = self.module.model.n_modules
        fix, ax = plt.subplots(n_modules, 1, figsize = (6, n_modules*4))
        for m in range(n_modules):
            print('Computing velocity produced by Module ' + str(m) + ' ...')
            with contextlib.redirect_stdout(io.StringIO()):
                mu_m = mu_mRNA_continousAlpha_globalTime_twoStates(
                            torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:]),
                            torch.tensor(0., dtype = torch.float),
                            torch.tensor(self.samples['post_sample_means']['beta_g']),
                            torch.tensor(self.samples['post_sample_means']['gamma_g']),
                            torch.tensor(self.samples['post_sample_means']['lam_mi'][m,:]),
                            torch.tensor(self.samples['post_sample_means']['T_c'][:,:,0]),
                            torch.tensor(self.samples['post_sample_means']['T_mON'][:,:,m]),
                            torch.tensor(self.samples['post_sample_means']['T_mOFF'][:,:,m]),
                            torch.zeros((self.module.model.n_obs, self.module.model.n_vars)))
                count_sum = torch.sum(torch.sum(mu_m, axis = 1), axis = -1)
                n_problem_cells = torch.sum(count_sum == torch.min(count_sum))
                if  n_problem_cells > 3:
                    problem_cells_index = count_sum == torch.min(count_sum)
                    mu_m[problem_cells_index,:,0] = torch.tensor(np.random.sample(n_problem_cells), dtype = torch.float).unsqueeze(-1)*\
                    torch.tensor(self.samples['post_sample_means']['mu_expression'][problem_cells_index,:,0], dtype = torch.float)*torch.tensor(10**(-5), dtype = torch.float)
                    mu_m[problem_cells_index,:,1] = torch.tensor(np.random.sample(n_problem_cells), dtype = torch.float).unsqueeze(-1)*\
                    torch.tensor(self.samples['post_sample_means']['mu_expression'][problem_cells_index,:,1], dtype = torch.float)*torch.tensor(10**(-5), dtype = torch.float)
                adata.layers['Module ' + str(m) + 'Spliced Mean'] = mu_m[...,1]
                adata.layers['Module ' + str(m) + ' Velocity'] = torch.tensor(self.samples['post_sample_means']['beta_g']) * \
                mu_m[...,0] - torch.tensor(self.samples['post_sample_means']['gamma_g']) * mu_m[...,1]
                adata.uns['Module ' + str(m) + ' Velocity' + '_graph'] = self.compute_velocity_graph_Bergen2020(
                                                       adata, n_neighbours = None, full_posterior = False,
                                                       velocity_key = 'Module ' + str(m) + ' Velocity',
                                                       spliced_key = 'Module ' + str(m) + 'Spliced Mean')
                if plot:
                    scv.pl.velocity_embedding_stream(adata, basis='umap', save = False, vkey='Module ' + str(m) + ' Velocity',
                                                     **plotting_kwargs, show = False, ax = ax[m])
                    ax[m].set_title('Module ' + str(m) + '\n Velocity Graph UMAP Embedding')
                del adata.layers['Module ' + str(m) + 'Spliced Mean']
                del mu_m

            if delete:
                del adata.layers['Module ' + str(m) + ' Velocity']
                
        if save:
            plt.savefig(save)     
            
    def compute_and_plot_total_velocity(self, adata, delete = True, plot = True, save = None,
                                     plotting_kwargs = {"color": 'clusters', 'legend_fontsize': 10, 'legend_loc': 'right_margin'}):
        """
        Computes total RNA velocity, as well as associated "velocity graph" and 
        then plots results on a UMAP based on the method in:
        "Bergen et al. (2020), Generalizing RNA velocity to transient cell states through dynamical modeling"
        """
        print('Computing total RNAvelocity ...')

        with contextlib.redirect_stdout(io.StringIO()):
            adata.layers['Spliced Mean'] = self.samples['post_sample_means']['mu_expression'][...,1]
            adata.layers['Velocity'] = torch.tensor(self.samples['post_sample_means']['beta_g']) * \
            self.samples['post_sample_means']['mu_expression'][...,0] - \
            torch.tensor(self.samples['post_sample_means']['gamma_g']) * \
            self.samples['post_sample_means']['mu_expression'][...,1]
            adata.uns['Velocity' + '_graph'] = self.compute_velocity_graph_Bergen2020(
                                                   adata, n_neighbours = None, full_posterior = False,
                                                   velocity_key = 'Velocity',
                                                   spliced_key = 'Spliced Mean')
            if plot:
                fix, ax = plt.subplots(1, 1, figsize = (6, 4))
                scv.pl.velocity_embedding_stream(adata, basis='umap', save = False, vkey='Velocity',
                                                 **plotting_kwargs, show = False, ax = ax)
                if save:
                    plt.savefig(save)
                    
            del adata.layers['Spliced Mean']
            if delete:
                del adata.layers['Velocity']
                
    def compute_and_plot_total_velocity_scvelo(self, adata, delete = True, plot = True, save = None,
                                     plotting_kwargs = {"color": 'clusters', 'legend_fontsize': 10, 'legend_loc': 'right_margin'}):
        """
        Computes total RNA velocity, as well as associated "velocity graph" and 
        then plots results on a UMAP based on the method in:
        "Bergen et al. (2020), Generalizing RNA velocity to transient cell states through dynamical modeling"
        """
        print('Computing total RNAvelocity ...')

        with contextlib.redirect_stdout(io.StringIO()):
            adata.layers['Mu'] = self.samples['post_sample_means']['mu_expression'][...,0]
            adata.layers['Ms'] = self.samples['post_sample_means']['mu_expression'][...,1]
            adata.layers['velocity'] = torch.tensor(self.samples['post_sample_means']['beta_g']) * \
            self.samples['post_sample_means']['mu_expression'][...,0] - \
            torch.tensor(self.samples['post_sample_means']['gamma_g']) * \
            self.samples['post_sample_means']['mu_expression'][...,1]
            scv.pp.neighbors(adata)
            scv.tl.velocity_graph(adata, vkey = 'velocity')
            scv.tl.velocity_embedding(adata, vkey = 'velocity')

            if plot:
                fix, ax = plt.subplots(1, 1, figsize = (6, 4))
                scv.pl.velocity_embedding_stream(adata, basis='umap', save = False, vkey='velocity',
                                                 **plotting_kwargs, show = False, ax = ax)
                if save:
                    plt.savefig(save)
                    
            del adata.layers['Ms']
            del adata.layers['Mu']
            if delete:
                del adata.layers['velocity']

    def compare_module_activation(self, adata, chosen_modules, time_max = None, time_min = 0,
                                 save = None, ncol = 1):
        n_modules = self.module.model.n_modules
        fig, ax = plt.subplots(1, 1, figsize=(18, 5))
        for m in chosen_modules:
            T_c = torch.tensor(0.).unsqueeze(-1).unsqueeze(-1)
            Tmax = self.samples['post_sample_means']['Tmax']
            count = 0
            fraction = 0
            fraction_list = [fraction]
            T_c_list = [0]
            ss_spliced = torch.sum(torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:])\
                                   /torch.tensor(self.samples['post_sample_means']['gamma_g']))
            abundance = torch.sum(mu_mRNA_continousAlpha_globalTime_twoStates(
                    torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:]),
                    torch.tensor(0.),
                    torch.tensor(self.samples['post_sample_means']['beta_g']),
                    torch.tensor(self.samples['post_sample_means']['gamma_g']),
                    torch.tensor(self.samples['post_sample_means']['lam_mi'][m,:]),
                    torch.tensor(self.samples['post_sample_means']['T_c'][:,:,0]),
                    torch.tensor(np.mean(self.samples['post_sample_means']['T_mON'][:,:,m])),
                    torch.tensor(np.mean(self.samples['post_sample_means']['T_mOFF'][:,:,m])),
                    torch.zeros((self.module.model.n_obs, self.module.model.n_vars)))[...,1], axis = -1)
            ax.scatter(self.samples['post_sample_means']['T_c'][:,:,0].flatten() - np.min(self.samples['post_sample_means']['T_c'][:,:,0].flatten()), abundance, s = 10, label = 'Module ' + str(m))
            ax.set_xlabel('Time (hours)')
            ax.set_ylabel('Total Spliced UMI Counts')
            if time_max or time_min > 0:
                ax.set_xlim(time_min, time_max)
            ax.legend(frameon=False, ncol = ncol)
            ax.set_title('Module Activation Across Cells In Dataset')
            if save:
                plt.savefig(save) 
            
    def plot_technical_variables(self, adata, save = False):
        '''Plots posterior of technical variables in the model.'''
        fig, ax = plt.subplots(3, 2, figsize = (12, 9))
        ax[0,0].scatter([str(x) for x in np.unique(adata.obs['_scvi_batch'])],
                      self.samples['post_sample_means']['detection_mean_y_e'], s = 150, c = 'black')
        ax[0,0].set_xlabel('Batch Number')
        ax[0,0].set_ylabel('Relative Detection Efficiency')
        ax[0,0].set_title('Mean Relative Detection Efficiency in each batch')
        detection_y_i = self.samples['post_sample_means']['detection_y_i']
        ax[0,1].hist(self.samples['post_sample_means']['detection_y_c'].flatten()*detection_y_i[0,0,0],
                     bins = 100, label = 'unspliced', alpha = 0.75, color = 'red')
        ax[0,1].hist(self.samples['post_sample_means']['detection_y_c'].flatten()*detection_y_i[0,0,1],
                     bins = 100, label = 'spliced', alpha = 0.75, color = 'blue')
        ax[0,1].legend(frameon=False)
        ax[0,1].set_xlabel('Relative Detection Efficiency')
        ax[0,1].set_ylabel('Number of Cells')
        ax[0,1].set_title('Relative Detection Efficiency across cells')
        ax[1,0].scatter([str(x) for x in np.unique(adata.obs['_scvi_batch'])],
                      self.samples['post_sample_means']['s_g_gene_add_mean'][...,0],
                      s = 150, c = 'red', label = 'unspliced')
        ax[1,0].scatter([str(x) for x in np.unique(adata.obs['_scvi_batch'])],
                      self.samples['post_sample_means']['s_g_gene_add_mean'][...,1],
                      s = 150, c = 'blue', label = 'spliced')
        ax[1,0].legend(frameon=False)
        ax[1,0].set_xlabel('Batch Number')
        ax[1,0].set_ylabel('Expected Ambient RNA')
        ax[1,0].set_title('Mean Ambient RNA counts in each batch')
        ax[1,1].hist(np.log10(self.samples['post_sample_means']['s_g_gene_add'][...,0].flatten()),
                     bins = 100, alpha = 0.75, label = 'unspliced', color = 'red')
        ax[1,1].hist(np.log10(self.samples['post_sample_means']['s_g_gene_add'][...,1].flatten()),
                     bins = 100, alpha = 0.75, label = 'spliced', color = 'blue')
        ax[1,1].legend(frameon=False)
        ax[1,1].set_xlabel('log10 Expected Ambient RNA Counts')
        ax[1,1].set_ylabel('Number of Genes')
        ax[1,1].set_title('log10 Ambient RNA across genes')
        ax[2,1].hist(np.log10(1./self.samples['post_sample_means']['stochastic_v_ag_inv'][...,0].flatten()**2),
                     bins = 100, alpha = 0.75, label = 'unspliced', color = 'red')
        ax[2,1].hist(np.log10(1./self.samples['post_sample_means']['stochastic_v_ag_inv'][...,1].flatten()**2),
                     bins = 100, alpha = 0.75, label = 'spliced', color = 'blue')
        ax[2,1].set_xlabel('log10 Overdispersion Factor')
        ax[2,1].set_ylabel('Number of Genes')
        ax[2,1].set_title('log10 Overdispersion factor across genes \n (smaller = more variance)')
        ax[2,0].hist(self.samples['post_sample_means']['detection_y_gi'][...,0].flatten(),
                     bins = 100, alpha = 0.75, label = 'unspliced', color = 'red')
        ax[2,0].hist(self.samples['post_sample_means']['detection_y_gi'][...,1].flatten(),
                     bins = 100, alpha = 0.75, label = 'spliced', color = 'blue')
        ax[2,0].set_xlabel('Relative Detection Efficiency')
        ax[2,0].set_ylabel('Number of Genes')
        ax[2,0].set_title('Relative Detection Efficiency across genes')
        ax[2,0].legend(frameon=False)
        plt.tight_layout()
        if save:
            plt.savefig(save)
    
    def view_history(self):
        """
        View training history over various training windows to assess convergence or spot potential training problems.
        """
        def generatePlots():
            yield
            self.plot_history()
            yield
            self.plot_history(int(np.round(self.max_epochs/8)))
            yield
            self.plot_history(int(np.round(self.max_epochs/4)))
            yield
            self.plot_history(int(np.round(self.max_epochs/2)))
        multiplot_from_generator(generatePlots(), 4)

    def get_module_top_features(self, adata, background, species = 'Mouse', p_adj_cutoff = 0.01, n_top_genes = None):
        '''Returns dataframe with top Genes, TFs and GO terms of each module'''
        tab = pd.DataFrame(columns = ('Module Number', 'Genes Ranked', 'TFs Ranked', 'Terms Ranked'))
        tab['Module Number'] = list(range(self.module.model.n_modules))
        if species == 'Human':
            TFs = np.array(pd.read_csv(c2f.__file__[:-11] + 'Human_TFs.txt', header=None, index_col=False)).flatten()
        elif species == 'Mouse':
            TFs = np.array(pd.read_csv(c2f.__file__[:-11] + 'Mouse_TFs.txt', header=None, index_col=False)).flatten()
        TFs = np.array([tf for tf in TFs if tf in adata.var_names])
        gene_by_module_weight = torch.zeros((self.module.model.n_modules, self.module.model.n_vars))
        gene_by_module_sorted = np.empty((self.module.model.n_modules, self.module.model.n_vars), dtype=object)
        TF_by_module_sorted = np.empty((self.module.model.n_modules, len(TFs)), dtype=object)
        TF_boolean = np.array([g in TFs for g in adata.var_names])
        inferred_total = torch.sum(torch.tensor(self.samples['post_sample_means']['mu_expression'])[...,1], axis = 0)
        for m in range(self.module.model.n_modules):
            mu_m = mu_mRNA_continousAlpha_globalTime_twoStates(
                torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:]),
                torch.tensor(0.),
                torch.tensor(self.samples['post_sample_means']['beta_g']),
                torch.tensor(self.samples['post_sample_means']['gamma_g']),
                torch.tensor(self.samples['post_sample_means']['lam_mi'][m,:]),
                torch.tensor(self.samples['post_sample_means']['T_c'][:,:,0]),
                torch.tensor(self.samples['post_sample_means']['T_mON'][:,:,m]),
                torch.tensor(self.samples['post_sample_means']['T_mOFF'][:,:,m]),
                torch.zeros((self.module.model.n_obs, self.module.model.n_vars)))
            gene_by_module_weight[m,:] = torch.sum(mu_m[...,1], axis = 0)/inferred_total
            gene_by_module_sorted[m,:] = adata.var_names[np.argsort(-1*gene_by_module_weight[m,:])]
            TF_by_module_sorted[m,:] = adata.var_names[TF_boolean][np.argsort(-1*gene_by_module_weight[m,TF_boolean])]
            tab.iloc[m,1] = ', '.join(list(gene_by_module_sorted[m,:]))
            tab.iloc[m,2] = ', '.join(list(TF_by_module_sorted[m,:]))

        ### Select n_genes/n_modules top genes for each module
        ### Find enriched GO terms
        n_modules = self.module.model.n_modules
        results = []
        if not n_top_genes:
            n_top_genes = int(self.module.model.n_vars/n_modules/2)
        for m in range(n_modules):
            gene_list = list(gene_by_module_sorted[m,:n_top_genes])
            if species == 'Mouse':
                enr = gp.enrichr(gene_list=gene_list,
                                 background = background,
                         gene_sets=['GO_Biological_Process_2021'], # 'GO_Cellular_Component_2021', 'KEGG_2019_Mouse'
                         organism='mouse', # don't forget to set organism to the one you desired! e.g. Yeast
                         outdir=None, # don't write to disk
                        )
            elif species == 'Human':
                enr = gp.enrichr(gene_list=gene_list,
                         background = background,
                     gene_sets=['GO_Biological_Process_2021', 'GO_Cellular_Component_2021', 'KEGG_2021_Human'],
                     organism='human', # don't forget to set organism to the one you desired! e.g. Yeast
                     outdir=None, # don't write to disk
                    )
            tab.iloc[m,3] = ', '.join(list(enr.results.loc[enr.results['Adjusted P-value'] < p_adj_cutoff,:]['Term']))
            results += [enr.results.loc[enr.results['Adjusted P-value'] < p_adj_cutoff,:]]
        ### Save topGenes, topTFs and topGOterms in dataframe.
        return tab, results
    
    def plot_top_features(self, adata, tab, chosen_modules, mode = 'all genes',
                      n_top_features = 3, save = False, process = True):
        if process:
            print('Reprocessing adata.X, set process = False if this is not desired.')
            adata.X = adata.layers['unspliced'] + adata.layers['spliced']
            sc.pp.normalize_total(adata, target_sum=1e4)
            sc.pp.log1p(adata)
            sc.pp.scale(adata, max_value=10)
        fig, ax, = plt.subplots(len(chosen_modules), n_top_features, figsize = (5*n_top_features, 4*len(chosen_modules)))
        for i in range(len(chosen_modules)):
            m = chosen_modules[i]
            if mode == 'all genes':
                for_plotting = tab['Genes Ranked'].iloc[m].replace(" ", "").split(',')[:n_top_features]
            elif mode == 'TFs':
                for_plotting = tab['TFs Ranked'].iloc[m].replace(" ", "").split(',')[:n_top_features]
            for j in range(n_top_features):
                sc.pl.umap(adata, color = for_plotting[j], legend_loc = 'right margin',
                            size = 200, ncols = n_top_features, show = False, ax = ax[i,j])
        if save:
            plt.savefig(save)
            
    def plot_module_summary_statistics_2(self, adata, chosen_modules, chosen_clusters,
                                     marker_genes, marker_TFs, cluster_key = 'clusters', save = None):
        'tbd'
        adata.X = adata.layers['unspliced'] + adata.layers['spliced']
        sc.pp.normalize_total(adata, target_sum=1e4)
        sc.pp.log1p(adata)
        sc.pp.scale(adata, max_value=10)
        plt.rcParams.update({'font.size': 14})
        subset = np.array([c in chosen_clusters for c in adata.obs[cluster_key]])
        plt.scatter(1, 1, label='Induction', s = 100, c='lightgreen')
        plt.scatter(1, 1, label='ON', s = 100, c='lime')
        plt.scatter(1, 1, label='Repression', s = 100, c='orange')
        plt.scatter(1, 1, label='OFF', s = 100, c='grey')
        plt.legend()
        if save:
            plt.savefig(save[:-4] + '_legend.pdf')
        plt.show()
        fig, ax = plt.subplots(4, len(chosen_modules), figsize = (3*len(chosen_modules), 20))
        adata = adata[subset,:]
        for i in range(len(chosen_modules)):
            m = chosen_modules[i]
            sc.pl.umap(adata, color = ['Module ' + str(m) + ' Activation'], legend_loc = None,
                        size = 200, color_map = 'viridis', ax = ax[0,i], show = False, s = 300)
            sc.pl.umap(adata, color = ['Module ' + str(m) + ' State'], 
                        size = 200, ax = ax[1,i], show = False, legend_fontsize = 'x-large', s = 300, legend_loc = 'right_margin',
                       palette = {'ON': 'lime', 'OFF': 'grey', 'Induction': 'lightgreen', 'Repression': 'orange'})
            sc.pl.umap(adata, color = marker_genes[i], legend_loc = 'right margin',
                            size = 200, show = False, ax = ax[2,i])
            sc.pl.umap(adata, color = marker_TFs[i], legend_loc = 'right margin',
                            size = 200, show = False, ax = ax[3,i])
            plt.tight_layout()
        if save:
            plt.savefig(save)  
    
    def example_module_activation(self, adata, chosen_module, time_max = None, time_min = 0,
                                 save = None):
        fig, ax = plt.subplots(1, 1, figsize=(10, 5))
        m = chosen_module
        n_obs = 10000
        T_c_min = torch.min(torch.tensor(self.samples['post_sample_means']['T_c']))
        T_c_max = torch.max(torch.tensor(self.samples['post_sample_means']['T_c']))
        T_c = torch.arange(T_c_min, T_c_max, (T_c_max - T_c_min)/n_obs).unsqueeze(-1).unsqueeze(-1)
        Tmax = self.samples['post_sample_means']['Tmax']
        count = 0
        fraction = 0
        fraction_list = [fraction]
        ss_spliced = torch.sum(torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:])\
                               /torch.tensor(self.samples['post_sample_means']['gamma_g']))
        abundance = torch.sum(mu_mRNA_continousAlpha_globalTime_twoStates(
                torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:]),
                torch.tensor(0.),
                torch.tensor(self.samples['post_sample_means']['beta_g']),
                torch.tensor(self.samples['post_sample_means']['gamma_g']),
                torch.tensor(self.samples['post_sample_means']['lam_mi'][m,:]),
                T_c[:,:,0],
                torch.tensor(np.mean(self.samples['post_sample_means']['T_mON'][:,:,m])),
                torch.tensor(np.mean(self.samples['post_sample_means']['T_mOFF'][:,:,m])),
                torch.zeros((n_obs+1, self.module.model.n_vars)))[...,1], axis = -1)
        abundance2 = torch.sum(mu_mRNA_continousAlpha_globalTime_twoStates(
                torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:]),
                torch.tensor(0.),
                torch.tensor(self.samples['post_sample_means']['beta_g']),
                torch.tensor(self.samples['post_sample_means']['gamma_g']),
                torch.tensor(self.samples['post_sample_means']['lam_mi'][m,:]),
                T_c[:,:,0],
                torch.tensor(np.mean(self.samples['post_sample_means']['T_mON'][:,:,m])),
                torch.tensor(np.mean(self.samples['post_sample_means']['T_mOFF'][:,:,m]))*1000.,
                torch.zeros((n_obs+1, self.module.model.n_vars)))[...,1], axis = -1)
        steady_state = torch.sum(torch.tensor(self.samples['post_sample_means']['A_mgON'][m,:])/torch.tensor(self.samples['post_sample_means']['gamma_g']))
        plt.axhspan(xmin = 0, xmax = 1, ymin = steady_state*0.95, ymax = steady_state,
                    facecolor='lime', alpha=0.5)
        plt.axhspan(xmin = 0, xmax = (np.mean(self.samples['post_sample_means']['T_mOFF'][:,:,m])-time_min)/(time_max-time_min), ymin = steady_state*0.05, ymax = steady_state*0.95,
                    facecolor='lightgreen', alpha=0.5)
        plt.axhspan(xmin = (np.mean(self.samples['post_sample_means']['T_mOFF'][:,:,m])-time_min)/(time_max-time_min), xmax = 1,
                    ymin = steady_state*0.05, ymax = steady_state*0.95,
                    facecolor='orange', alpha=0.5)
        plt.axhspan(xmin = 0, xmax = 1,
                    ymin = steady_state*0, ymax = steady_state*0.05,
                    facecolor='grey', alpha=0.5)
        ax.scatter(T_c,
                   abundance2, s = 3, label = 'Module ' + str(m), c = 'grey', alpha = 0.25)
        ax.scatter(T_c,
                   abundance, s = 5, label = 'Module ' + str(m), c = 'black')
        ax.axhline(xmin = 0, xmax = time_max, y = steady_state, linestyle = '--', linewidth = 1, c = 'black')
        ax.axhline(xmin = 0, xmax = time_max, y = steady_state*0.05, linestyle = '--', linewidth = 1, c = 'black')
        ax.axhline(xmin = 0, xmax = time_max, y = steady_state*0.95, linestyle = '--', linewidth = 1, c = 'black')
        ax.axvline(ymin = 0, ymax = np.float(steady_state), x = np.mean(self.samples['post_sample_means']['T_mOFF'][:,:,m]), linestyle = '--', linewidth = 1, c = 'black')
        ax.set_xlabel('Time (hours)')
        ax.set_ylabel('Total Spliced UMI Counts')
        ax.set_ylim(-10, steady_state+20)
        if time_max or time_min > 0:
            ax.set_xlim(time_min, time_max)
        ax.set_title('Example Module Activation')
        if save:
            plt.savefig(save)   

    def plot_genes(self, adata, chosen_clusters, marker_genes, cluster_key = 'clusters', save = None):
        'Weight, Activation, Velocity, Switch ON/OFF time (histogram)'
        import matplotlib.pyplot as plt
        adata.X = adata.layers['unspliced'] + adata.layers['spliced']
        sc.pp.normalize_total(adata, target_sum=1e4)
        sc.pp.log1p(adata)
        sc.pp.scale(adata, max_value=10)
        plt.rcParams.update({'font.size': 12})
        subset = np.array([c in chosen_clusters for c in adata.obs[cluster_key]])
        fig, ax = plt.subplots(1, len(marker_genes), figsize = (3*len(marker_genes), 5))
        adata = adata[subset,:]
        for i in range(len(marker_genes)):
            sc.pl.umap(adata, color = marker_genes[i], legend_loc = 'right margin',
                            size = 200, show = False, ax = ax[i])
            plt.tight_layout()
        if save:
            plt.savefig(save) 