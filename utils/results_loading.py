import hail as hl
from gnomad.utils import *
from ukb_common.resources.generic import *


def format_pheno_dir(pheno):
    return pheno.replace("/", "_")


def get_top_p_from_mt(mt, p, return_ht = True):
    top_p_hit = hl.agg.filter(hl.is_defined(p) & ~hl.is_nan(p),
                              hl.agg.take(mt.entry.annotate(**mt.col), 1, ordering=p))
    mt = mt.annotate_rows(top_p=hl.or_missing(hl.len(top_p_hit) > 0, top_p_hit[0]))
    if return_ht:
        ht = mt.rows()
        return ht.transmute(**ht.top_p)
    else:
        return mt


PLOF_CSQS = ["transcript_ablation", "splice_acceptor_variant",
             "splice_donor_variant", "stop_gained", "frameshift_variant"]

MISSENSE_CSQS = ["stop_lost", "start_lost", "transcript_amplification",
                 "inframe_insertion", "inframe_deletion", "missense_variant"]

SYNONYMOUS_CSQS = ["stop_retained_variant", "synonymous_variant"]

OTHER_CSQS = ["mature_miRNA_variant", "5_prime_UTR_variant",
              "3_prime_UTR_variant", "non_coding_transcript_exon_variant", "intron_variant",
              "NMD_transcript_variant", "non_coding_transcript_variant", "upstream_gene_variant",
              "downstream_gene_variant", "TFBS_ablation", "TFBS_amplification", "TF_binding_site_variant",
              "regulatory_region_ablation", "regulatory_region_amplification", "feature_elongation",
              "regulatory_region_variant", "feature_truncation", "intergenic_variant"]

# Unused: protein_altering_variant, incomplete_terminal_codon_variant, coding_sequence_variant
# TODO: question, what to do with: "splice_region_variant"
# TODO: question, "missense-damaging" vs "damaging_missense"


def annotation_case_builder(worst_csq_by_gene_canonical_expr, use_loftee: bool = True, use_polyphen_and_sift: bool = False,
                            strict_definitions: bool = False):
    case = hl.case(missing_false=True)
    if use_loftee:
        case = (case
                .when(worst_csq_by_gene_canonical_expr.lof == 'HC', 'pLoF')
                .when(worst_csq_by_gene_canonical_expr.lof == 'LC', 'LC'))
    else:
        case = case.when(hl.set(PLOF_CSQS).contains(worst_csq_by_gene_canonical_expr.most_severe_consequence), 'pLoF')
    if use_polyphen_and_sift:
        case = (case
                .when(missense.contains(mt.vep.worst_csq_for_variant_canonical.most_severe_consequence) &
                      (mt.vep.worst_csq_for_variant_canonical.polyphen_prediction == "probably_damaging") &
                      (mt.vep.worst_csq_for_variant_canonical.sift_prediction == "deleterious"), "damaging_missense")
                .when(missense.contains(mt.vep.worst_csq_for_variant_canonical.most_severe_consequence), "other_missense"))
    else:
        if strict_definitions:
            case = case.when(worst_csq_by_gene_canonical_expr.most_severe_consequence == 'missense_variant', 'missense')
        else:
            case = case.when(hl.set(MISSENSE_CSQS).contains(worst_csq_by_gene_canonical_expr.most_severe_consequence), 'missense')
    if strict_definitions:
        case = case.when(worst_csq_by_gene_canonical_expr.most_severe_consequence == 'synonymous_variant', 'synonymous')
    else:
        case = case.when(hl.set(SYNONYMOUS_CSQS).contains(worst_csq_by_gene_canonical_expr.most_severe_consequence), 'synonymous')
    case=  case.when(hl.set(OTHER_CSQS).contains(worst_csq_by_gene_canonical_expr.most_severe_consequence), 'non-coding')
    return case.or_missing()


def get_vep_formatted_data(ukb_vep_path: str):
    ht = hl.read_table(ukb_vep_path)
    ht = process_consequences(ht)
    ht = ht.explode(ht.vep.worst_csq_by_gene_canonical)
    return ht.select(
        gene=ht.vep.worst_csq_by_gene_canonical.gene_symbol,
        annotation=annotation_case_builder(ht.vep.worst_csq_by_gene_canonical))


def load_variant_data(directory: str, pheno_key_dict, ukb_vep_path: str, extension: str = 'single.txt',
                      n_cases: int = -1, n_controls: int = -1, heritability: float = -1.0,
                      saige_version: str = 'NA', overwrite: bool = False):
    output_ht_path = f'{directory}/variant_results.ht'
    ht = hl.import_table(f'{directory}/*.{extension}', delimiter=' ', impute=True)
    print(f'Loading: {directory}/*.{extension} ...')
    marker_id_col = 'markerID' if extension == 'single.txt' else 'SNPID'
    locus_alleles = ht[marker_id_col].split('_')
    if n_cases == -1: n_cases = hl.null(hl.tint)
    if n_controls == -1: n_controls = hl.null(hl.tint)
    if heritability == -1.0: heritability = hl.null(hl.tfloat)
    if saige_version == 'NA': saige_version = hl.null(hl.tstr)

    ht = ht.key_by(locus=hl.parse_locus(locus_alleles[0]), alleles=locus_alleles[1].split('/'),
                   **pheno_key_dict).distinct().naive_coalesce(50)
    if marker_id_col == 'SNPID':
        ht = ht.drop('CHR', 'POS', 'rsid', 'Allele1', 'Allele2')
    ht = ht.transmute(Pvalue=ht['p.value']).annotate_globals(
        n_cases=n_cases, n_controls=n_controls, heritability=heritability, saige_version=saige_version)
    ht = ht.annotate(**get_vep_formatted_data(ukb_vep_path)[
        hl.struct(locus=ht.locus, alleles=ht.alleles)])  # TODO: fix this for variants that overlap multiple genes
    ht = ht.checkpoint(output_ht_path, overwrite=overwrite, _read_if_exists=not overwrite).drop('n_cases', 'n_controls', 'heritability')
    # mt = ht.to_matrix_table(['locus', 'alleles'], list(pheno_key_dict.keys()),
    #                         [marker_id_col, 'gene', 'annotation'], []).annotate_cols(
    #     n_cases=n_cases, n_controls=n_controls, heritability=heritability)
    # mt.checkpoint(output_ht_path.replace('.ht', '.mt'), overwrite=overwrite, _read_if_exists=not overwrite)


def load_gene_data(directory: str, pheno_key_dict, gene_ht_map_path: str,
                   n_cases: int = -1, n_controls: int = -1, heritability: float = -1.0, saige_version: str = 'NA',
                   overwrite: bool = False):
    output_ht_path = f'{directory}/gene_results.ht'
    print(f'Loading: {directory}/*.gene.txt ...')
    types = {f'Nmarker_MACCate_{i}': hl.tint32 for i in range(1, 9)}
    types.update({x: hl.tfloat64 for x in ('Pvalue', 'Pvalue_Burden', 'Pvalue_SKAT', 'Pvalue_skato_NA', 'Pvalue_burden_NA', 'Pvalue_skat_NA')})
    ht = hl.import_table(f'{directory}/*.gene.txt', delimiter=' ', impute=True, types=types)
    if n_cases == -1: n_cases = hl.null(hl.tint)
    if n_controls == -1: n_controls = hl.null(hl.tint)
    if heritability == -1.0: heritability = hl.null(hl.tfloat)
    if saige_version == 'NA': saige_version = hl.null(hl.tstr)

    fields = ht.Gene.split('_')
    gene_ht = hl.read_table(gene_ht_map_path).select('interval').distinct()
    ht = ht.key_by(gene_id=fields[0], gene_symbol=fields[1], annotation=fields[2],
                   **pheno_key_dict).drop('Gene').naive_coalesce(10).annotate_globals(
        n_cases=n_cases, n_controls=n_controls, heritability=heritability, saige_version=saige_version)
    ht = ht.annotate(total_variants=hl.sum([v for k, v in list(ht.row_value.items()) if 'Nmarker' in k]),
                     interval=gene_ht.key_by('gene_id')[ht.gene_id].interval)
    ht = ht.checkpoint(output_ht_path, overwrite=overwrite, _read_if_exists=not overwrite).drop('n_cases', 'n_controls')
    # mt = ht.to_matrix_table(['gene_symbol', 'gene_id', 'annotation', 'interval'],
    #                         list(pheno_key_dict.keys()), [], []).annotate_cols(
    #     n_cases=n_cases, n_controls=n_controls, heritability=heritability)
    # mt.checkpoint(output_ht_path.replace('.ht', '.mt'), overwrite=overwrite, _read_if_exists=not overwrite)


def get_cases_and_controls_from_log(log_format):
    """
    'gs://path/to/result_chr{chrom}_000000001.variant.log'
    """
    cases = controls = -1
    for chrom in range(10, 23):
        try:
            with hl.hadoop_open(log_format.format(chrom=chrom)) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('Analyzing'):
                        fields = line.split()
                        if len(fields) == 6:
                            try:
                                cases = int(fields[1])
                                controls = int(fields[4])
                                break
                            except ValueError:
                                logger.warn(f'Could not load number of cases or controls from {line}.')
                    elif line.endswith('samples were used in fitting the NULL glmm model and are found in sample file') or \
                            line.endswith('samples have been used to fit the glmm null model'):
                        # This is ahead of the case/control count line ("Analyzing ...") above so this should be ok
                        fields = line.split()
                        try:
                            cases = int(fields[0])
                        except ValueError:
                            logger.warn(f'Could not load number of cases or controls from {line}.')
            return cases, controls
        except:
            pass
    return cases, controls


def get_heritability_from_log(log_file, quantitative_trait: bool = False):
    import math
    heritability = -1
    with hl.hadoop_open(log_file) as f:
        for line in f:
            if line.startswith('Final'):
                fields = line.strip().split()
                if len(fields) == 4:
                    try:
                        tau = float(fields[2])
                        if quantitative_trait:
                            tau1 = float(fields[1])
                            heritability = tau / (tau1 + tau)
                        else:
                            heritability = tau / (tau + math.pi ** 2 / 3)
                        break
                    except:
                        logger.warn(f'Could not load heritability from {line}.')
    return heritability


def get_saige_version_from_log(null_glmm_log):
    version = 'NA'
    with hl.hadoop_open(null_glmm_log) as f:
        for line in f:
            if line.startswith('other attached packages:'):
                try:
                    line2 = f.readline()
                    packages = line2.strip().split()
                    version = [x for x in packages if 'SAIGE' in x][0]
                except:
                    logger.warning(f'Could not load version number from {line2} in {null_glmm_log}.')
    return version


def get_inverse_normalize_status(null_glmm_log):
    status = 'Unknown'
    with hl.hadoop_open(null_glmm_log) as f:
        for line in f:
            if line.startswith('$invNormalize'):
                try:
                    status = f.readline().strip().split()[1]
                except:
                    logger.warning(f'Could not load inv_norm status from {line} in {null_glmm_log}.')
    return status.capitalize()


def get_saige_timing_grep(all_files):
    try:
        grep_results = hl.grep('Analysis took', all_files, max_count=int(1e8), show=False)
    except hl.utils.java.FatalError:
        return
    if sum([len(x) for x in grep_results.values()]) > 5e7:
        logger.warning(f'Got more than 5e7 values in {all_files[0]}, etc. Check this!')
    for log, result in grep_results.items():
        try:
            timing = float(result[0].split()[2])
        except:
            logger.warning(f'Could not load timing from {result} in {log}.')
            continue
        chrom, pos = log.rsplit('.', 2)[0].rsplit('_', 2)[1:3]
        yield f'{chrom}:{pos}', timing


def get_null_model_timing(null_glmm_log):
    cpu = wall = 'NA'
    with hl.hadoop_open(null_glmm_log) as f:
        for line in f:
            if line.startswith('t_end - t_begin'):
                try:
                    f.readline()
                    line2 = f.readline()
                    cpu, _, wall = line2.strip().split()
                except:
                    logger.warning(f'Could not load null model timings from {line2} in {null_glmm_log}.')
    return cpu, wall


def union_mts_by_tree(all_mts, temp_dir, debug=False):
    chunk_size = int(len(all_mts) ** 0.5) + 1
    outer_mts = []
    for i in range(chunk_size):
        if i * chunk_size >= len(all_mts): break
        mt = all_mts[i * chunk_size]
        for j in range(1, chunk_size):
            if i * chunk_size + j >= len(all_mts): break
            try:
                mt = mt.union_cols(all_mts[i * chunk_size + j], row_join_type='outer')
            except:
                if debug:
                    print(f'problem with {i * chunk_size} and {i * chunk_size + j}')
                    mt.describe()
                    all_mts[i * chunk_size + j].describe()
                raise
        outer_mts.append(mt.checkpoint(f'{temp_dir}/temp_output_{i}.mt', overwrite=True))
    mt = outer_mts[0]
    for next_mt in outer_mts[1:]:
        mt = mt.union_cols(next_mt, row_join_type='outer')
    return mt


def union_hts_by_tree(all_hts, temp_dir, debug=False, inner_mode = 'overwrite'):
    chunk_size = int(len(all_hts) ** 0.5) + 1
    outer_hts = []
    for i in range(chunk_size):
        if i * chunk_size >= len(all_hts): break
        hts = all_hts[i * chunk_size:(i + 1) * chunk_size]
        try:
            if isinstance(hts[0], str):
                hts = list(map(lambda x: hl.read_table(x), hts))
            ht = hts[0].union(*hts[1:], unify=True)
        except:
            if debug:
                print(f'problem in range {i * chunk_size}-{i * chunk_size + chunk_size}')
                _ = [ht.describe() for ht in hts]
            raise
        outer_hts.append(ht.checkpoint(f'{temp_dir}/temp_output_{i}.ht', **{inner_mode: True}))
    return outer_hts[0].union(*outer_hts[1:], unify=True)


def get_files_in_parent_directory(parent_dir, fname: str = 'variant_results.ht'):
    all_outputs = []
    for directory in parent_dir:
        if not directory['is_dir']:
            continue
        file_path = f'{directory["path"]}/{fname}'
        if hl.hadoop_exists(f'{file_path}/_SUCCESS'):
            all_outputs.append(file_path)
    return all_outputs


def union_ht(all_hts, col_fields, pheno_dict, temp_dir, inner_mode: str = 'overwrite'):
    print(f'Unioning {len(all_hts)} HTs...')
    ht = union_hts_by_tree(all_hts, temp_dir, inner_mode=inner_mode)
    return ht.annotate(**pheno_dict[ht.key.select(*col_fields)])


def pull_out_col_keys(all_hts, row_keys, col_keys):
    rekeyed_hts = []
    for ht in all_hts:
        ht2 = ht.head(1)
        glob = ht2.aggregate(hl.agg.take(hl.struct(**{x: ht2[x] for x in col_keys}), 1)[0], _localize=False)
        rekeyed_hts.append(ht.key_by(*row_keys).drop(*col_keys).annotate_globals(**glob))
    return rekeyed_hts


def join_pheno_hts_to_mt(all_hts, row_keys, col_keys, temp_dir = None, inner_mode: str = 'overwrite',
                         repartition_final: int = None):
    rekeyed_hts = pull_out_col_keys(all_hts, row_keys, col_keys)
    mt = mwzj_hts_by_tree(rekeyed_hts, temp_dir, col_keys, debug=True,
                          inner_mode=inner_mode, repartition_final=repartition_final)
    print(f'Unioned MTs...')
    return mt


def unify_saige_ht_schema(ht, patch_case_control_count: str = ''):
    """

    :param Table ht:
    :param str patch_case_control_count: Path to file (hack to get cases and controls back if loading later)
    :return:
    :rtype: Table
    """
    if 'AF.Cases' not in list(ht.row):
        ht = ht.select('AC_Allele2', 'AF_Allele2', 'imputationInfo', 'N', 'BETA', 'SE', 'Tstat',
                       **{'p.value.NA': hl.null(hl.tfloat64), 'Is.SPA.converge': hl.null(hl.tint32),
                          'varT': ht.varT, 'varTstar': ht.varTstar, 'AF.Cases': hl.null(hl.tfloat64),
                          'AF.Controls': hl.null(hl.tfloat64), 'Pvalue': ht.Pvalue,
                          'gene': hl.or_else(ht.gene, ''), 'annotation': hl.or_else(ht.annotation, '')})
    else:
        ht = ht.select('AC_Allele2', 'AF_Allele2', 'imputationInfo', 'N', 'BETA', 'SE', 'Tstat',
                       'p.value.NA', 'Is.SPA.converge', 'varT', 'varTstar', 'AF.Cases',
                       'AF.Controls', 'Pvalue', gene=hl.or_else(ht.gene, ''), annotation=hl.or_else(ht.annotation, ''))
    if 'heritability' in list(ht.globals):
        ht = ht.drop('heritability')
    if 'saige_version' in list(ht.globals):
        ht = ht.drop('saige_version')

    if patch_case_control_count:
        if not ht.n_cases.collect()[0]:
            directory, tpc, _ = patch_case_control_count.rsplit('/', 2)
            trait_type, pc = tpc.split('-', 1)
            pheno, coding = pc.rsplit('-', 1)
            cases, controls = get_cases_and_controls_from_log(f'{directory}/{tpc}/result_{pheno}',
                                                              log_suffix='variant', chrom_prefix='')
            print(f'Patched pheno: {pheno}. Got {cases} cases and {controls} controls.')
            if cases == -1: cases = hl.null(hl.tint)
            if controls == -1: controls = hl.null(hl.tint)
            ht = ht.annotate_globals(n_cases=cases, n_controls=controls)
    return ht


def unify_saige_ht_variant_schema(ht):
    shared = ('markerID', 'AC', 'AF', 'N', 'BETA', 'SE', 'Tstat', 'varT', 'varTstar')
    new_floats = ('AF.Cases', 'AF.Controls')
    new_ints = ('N.Cases', 'N.Controls')
    shared_end = ('Pvalue', 'gene', 'annotation')
    if 'AF.Cases' not in list(ht.row):
        ht = ht.select(*shared, **{field: hl.null(hl.tfloat64) for field in new_floats},
                       **{field: hl.null(hl.tint32) for field in new_ints},
                       **{field: ht[field] for field in shared_end})
    else:
        ht = ht.select(*shared, *new_floats, *new_ints, *shared_end)
    return ht.annotate(SE=hl.float64(ht.SE), AC=hl.int32(ht.AC))


def unify_saige_burden_ht_schema(ht):
    shared = ('Pvalue', *(f'Nmarker_MACCate_{i}' for i in range(1, 9)), 'markerIDs', 'markerAFs',
              'Pvalue_Burden', 'Pvalue_SKAT', 'BETA_Burden', 'SE_Burden')
    new_floats = ('Pvalue.NA', 'Pvalue_Burden.NA', 'Pvalue_SKAT.NA', 'BETA_Burden.NA')
    new_strings = ('SE_Burden.NA', )
    shared_end = ('total_variants', 'interval')
    if 'Pvalue.NA' not in list(ht.row):
        ht = ht.select(*shared, **{field: hl.null(hl.tfloat64) for field in new_floats},
                       **{field: hl.null(hl.tstr) for field in new_strings},
                       **{field: ht[field] for field in shared_end})
    else:
        ht = ht.select(*shared, *new_floats, *new_strings, *shared_end)
    return ht.annotate(**{'SE_Burden': hl.float64(ht.SE_Burden), 'SE_Burden.NA': hl.float64(ht['SE_Burden.NA'])})


def get_n_even_intervals(n):
    ref = hl.default_reference()
    genome_size = sum(ref.lengths.values())
    partition_size = int(genome_size / n) + 1
    return list(map(
        lambda x: hl.Interval(hl.eval(hl.locus_from_global_position(x * partition_size)),
                              hl.eval(hl.locus_from_global_position(min(x * partition_size + partition_size, genome_size - 1)))),
        range(n)))


def mwzj_hts_by_tree(all_hts, temp_dir, globals_for_col_key, debug=False, inner_mode = 'overwrite',
                     repartition_final: int = None):
    chunk_size = int(len(all_hts) ** 0.5) + 1
    outer_hts = []

    checkpoint_kwargs = {inner_mode: True}
    if repartition_final is not None:
        intervals = get_n_even_intervals(repartition_final)
        checkpoint_kwargs['_intervals'] = intervals

    if debug: print(f'Running chunk size {chunk_size}...')
    for i in range(chunk_size):
        if i * chunk_size >= len(all_hts): break
        hts = all_hts[i * chunk_size:(i + 1) * chunk_size]
        if debug: print(f'Going from {i * chunk_size} to {(i + 1) * chunk_size} ({len(hts)} HTs)...')
        try:
            if isinstance(hts[0], str):
                hts = list(map(lambda x: hl.read_table(x), hts))
            ht = hl.Table.multi_way_zip_join(hts, 'row_field_name', 'global_field_name')
        except:
            if debug:
                print(f'problem in range {i * chunk_size}-{i * chunk_size + chunk_size}')
                _ = [ht.describe() for ht in hts]
            raise
        outer_hts.append(ht.checkpoint(f'{temp_dir}/temp_output_{i}.ht', **checkpoint_kwargs))
    ht = hl.Table.multi_way_zip_join(outer_hts, 'row_field_name_outer', 'global_field_name_outer')
    ht = ht.transmute(inner_row=hl.flatmap(lambda i:
                                           hl.cond(hl.is_missing(ht.row_field_name_outer[i].row_field_name),
                                                   hl.range(0, hl.len(ht.global_field_name_outer[i].global_field_name))
                                                   .map(lambda _: hl.null(ht.row_field_name_outer[i].row_field_name.dtype.element_type)),
                                                   ht.row_field_name_outer[i].row_field_name),
                                           hl.range(hl.len(ht.global_field_name_outer))))
    ht = ht.transmute_globals(inner_global=hl.flatmap(lambda x: x.global_field_name, ht.global_field_name_outer))
    mt = ht._unlocalize_entries('inner_row', 'inner_global', globals_for_col_key)
    return mt
