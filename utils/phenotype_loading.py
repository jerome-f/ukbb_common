import csv
import subprocess
import os
import tempfile
import hail as hl


def pre_process_data_dictionary(pheno_description_raw_path, pheno_description_path):
    """
    Convert Data_Dictionary_Showcase.csv to tsv to enable load into hail

    :param str pheno_description_raw_path: Input file
    :param str pheno_description_path: Parsed tsv file
    """
    local_pheno_description_path = '/tmp/Data_Dictionary_Showcase.csv'
    local_pheno_description_out_path = '/tmp/Data_Dictionary_Showcase.tsv'
    hl.hadoop_copy(pheno_description_raw_path, f'file://{local_pheno_description_path}')
    with open(local_pheno_description_path) as f, open(local_pheno_description_out_path, 'w') as g:
        reader = csv.reader(f)
        for line in reader:
            g.write('\t'.join(line) + '\n')
    hl.hadoop_copy(f'file://{local_pheno_description_out_path}', pheno_description_path)


def get_codings():
    """
    Read codings data from Duncan's repo and load into hail Table

    :return: Hail table with codings
    :rtype: Table
    """
    root = f'{tempfile.gettempdir()}/PHESANT'
    if subprocess.check_call(['git', 'clone', 'https://github.com/astheeggeggs/PHESANT.git', root]):
        raise Exception('Could not clone repo')
    hts = []
    coding_dir = f'{root}/WAS/codings'
    for coding_file in os.listdir(f'{coding_dir}'):
        hl.hadoop_copy(f'file://{coding_dir}/{coding_file}', f'{coding_dir}/{coding_file}')
        ht = hl.import_table(f'{coding_dir}/{coding_file}')
        if 'node_id' not in ht.row:
            ht = ht.annotate(node_id=hl.null(hl.tstr), parent_id=hl.null(hl.tstr), selectable=hl.null(hl.tstr))
        ht = ht.annotate(coding_id=hl.int(coding_file.split('.')[0].replace('coding', '')))
        hts.append(ht)
    full_ht = hts[0].union(*hts[1:]).key_by('coding_id', 'coding')
    return full_ht.repartition(10)


def pheno_ht_to_mt(pheno_ht: hl.Table, data_type: str):
    """
    Input Hail Table with lots of phenotype row fields, distill into
    MatrixTable with either categorical or continuous data types
    as entries

    :param Table pheno_ht: Input hail Table with phenotypes as row fields
    :param str data_type: one of "categorical" or "continuous"
    :return: Hail MatrixTable with phenotypes as entries
    :rtype: MatrixTable
    """
    if data_type == 'categorical':
        category_type = hl.int
        filter_type = {hl.tbool}
        value_type = hl.bool
    else:
        category_type = hl.str
        filter_type = {hl.tint, hl.tfloat}
        value_type = hl.float

    pheno_ht = pheno_ht.select(**{x: value_type(v) for x, v in pheno_ht.row_value.items() if v.dtype in filter_type})

    mt = pheno_ht.to_matrix_table_row_major(
        columns=list(pheno_ht.row_value), entry_field_name='value', col_field_name='phesant_pheno'
    )
    return mt.key_cols_by(
        pheno=hl.int(mt.phesant_pheno.split('_')[0]),
        coding=hl.cond(hl.len(mt.phesant_pheno.split('_')) > 1,
                       category_type(mt.phesant_pheno.split('_')[1]),
                       category_type(''))  # NB: This would error for categoricals if there were any missing a value
    )


def get_missing_codings(ht):
    """
    Download missing coding data files from UKB website

    :param Table ht: Input table with `meaning` and `coding_id`
    """
    missing_codings = ht.aggregate(hl.agg.filter(hl.is_missing(ht.meaning), hl.agg.collect_as_set(ht.coding_id)))
    import requests
    print(f'Missing: {missing_codings}')
    for coding in missing_codings:
        r = requests.post(url='http://biobank.ndph.ox.ac.uk/showcase/codown.cgi', data={'id': coding})
        with open(f'/tmp/coding{coding}.tsv', 'w') as f:
            f.write(r.text)


def get_phesant_reassignments(phesant_summary):
    """
    Helper function for add_coding_information.
    Parse PHESANT phenotype description data to get any coding reassignments.

    :param Table phesant_summary: Summary hail Table with PHESANT metadata
    :return: Table with reassignments
    :rtype: Table
    """
    phesant_summary = phesant_summary.annotate(
        reassign=phesant_summary['PHESANT.reassignments'].split(' ')[1].split('\|').map(lambda x: x.split('=')))
    ht = phesant_summary.explode('reassign')
    ht = ht.filter(ht.reassign[1] != 'NA')
    ht = ht.transmute(reassign_from=hl.int(ht.reassign[0]), reassign_to=hl.int(ht.reassign[1]))
    ht = ht.key_by(
        pheno=hl.int(ht.FieldID.split('_')[0]),
        coding=hl.or_missing(hl.len(ht.FieldID.split('_')) > 1, hl.int(ht.FieldID.split('_')[1]))
    )
    return ht.filter(ht.reassign_to == ht.coding)


def add_coding_information(mt: hl.MatrixTable, coding_ht: hl.Table, phesant_phenotype_info_path: str,
                           download_missing_codings: bool = False) -> hl.MatrixTable:
    """
    Add coding information from coding_ht as column annotations into mt

    :param MatrixTable mt: Input MT
    :param Table coding_ht: HT with coding information
    :param str phesant_phenotype_info_path: PHESANT phenotype metadata path
    :param bool download_missing_codings: Whether to download missing coding data
    :return: MT with coding information in column data
    :rtype: MatrixTable
    """
    mt = mt.annotate_cols(**coding_ht[(mt.coding_id, hl.str(mt.coding))])
    if download_missing_codings: get_missing_codings(mt.cols())
    phesant_summary = hl.import_table(phesant_phenotype_info_path, impute=True, missing='', key='FieldID')
    phesant_reassign = get_phesant_reassignments(phesant_summary)
    mt = mt.annotate_cols(recoding=hl.or_missing(
        hl.is_missing(mt.meaning), phesant_reassign[mt.col_key].reassign_from
    ))
    return mt.annotate_cols(**hl.cond(hl.is_defined(mt.meaning),
                                      hl.struct(**{x: mt[x] for x in list(coding_ht.row_value)}),
                                      coding_ht[(mt.coding_id, hl.str(mt.recoding))]),
                            )


def combine_datasets(mt_path_dict: dict, summary_tsv_path_dict: dict,
                     pheno_description_path: str, coding_ht_path: str, data_type: str = 'categorical'):
    """
    Combine "both sexes", female, and male MTs into one with multiple entry fields,
    adding phenotype descriptions and coding.

    :param dict mt_path_dict: Dict of MTs (includes `both_sexes_no_sex_specific`, `females`, `males`)
    :param dict summary_tsv_path_dict: Dict of summary TSVs (includes `both_sexes_no_sex_specific`, `females`, `males`)
    :param str pheno_description_path: Phenotype description TSV path
    :param str coding_ht_path: Coding hail Table path
    :param str data_type: One of "categorical" or "continuous"
    :return: MatrixTable with all 3 entries combined
    :rtype: MatrixTable
    """
    both_mt = hl.read_matrix_table(mt_path_dict['both_sexes_no_sex_specific'])
    female_mt = hl.read_matrix_table(mt_path_dict['females'])
    male_mt = hl.read_matrix_table(mt_path_dict['males'])

    description_ht = hl.import_table(pheno_description_path, impute=True, missing='', key='FieldID')
    description_ht = description_ht.transmute(coding_id=description_ht.Coding)

    both_mt = both_mt.annotate_cols(**description_ht[both_mt.pheno])
    female_mt = female_mt.annotate_cols(**description_ht[female_mt.pheno])
    male_mt = male_mt.annotate_cols(**description_ht[male_mt.pheno])

    if data_type == 'categorical':
        coding_ht = hl.read_table(coding_ht_path)
        both_mt = add_coding_information(both_mt, coding_ht, summary_tsv_path_dict['both_sexes_no_sex_specific'])
        female_mt = add_coding_information(female_mt, coding_ht, summary_tsv_path_dict['females'])
        male_mt = add_coding_information(male_mt, coding_ht, summary_tsv_path_dict['males'])

    mt = hl.experimental.full_outer_join_mt(both_mt, female_mt)
    mt = mt.select_entries(
        both_sexes=mt.left_entry.value,
        females=mt.right_entry.value,
    ).drop('left_row', 'right_row')
    # ht = mt.cols().persist()
    # assert ht.all(ht.left_col == ht.right_col)
    mt = mt.select_cols(both_sexes_pheno=mt.left_col.drop(*mt.col_key), females_pheno=mt.right_col.drop(*mt.col_key))

    mt = hl.experimental.full_outer_join_mt(mt, male_mt)
    mt = mt.select_entries(**mt.left_entry, males=mt.right_entry.value).drop('left_row', 'right_row')
    mt = mt.select_cols(**mt.left_col.drop(*mt.col_key), males_pheno=mt.right_col.drop(*mt.col_key))
    return mt


def load_icd_data(pre_phesant_data_path, icd_codings_path, temp_directory, force_overwrite_intermediate: bool = False):
    """
    Load raw (pre-PHESANT) phenotype data and extract ICD codes into hail MatrixTable with booleans as entries

    :param str pre_phesant_data_path: Input phenotype file
    :param str icd_codings_path: Input coding metadata
    :param str temp_directory: Temp bucket/directory to write intermediate file
    :param bool force_overwrite_intermediate: Whether to overwrite intermediate loaded file
    :return: MatrixTable with ICD codes
    :rtype: MatrixTable
    """
    code_locations = {
        'primary_codes': '41202',
        'secondary_codes': '41204',
        'external_codes': '41201',
        'cause_of_death_codes': '40001'
    }
    ht = hl.import_table(pre_phesant_data_path, impute=True, min_partitions=100, missing='', key='userId')
    ht = ht.checkpoint(f'{temp_directory}/pre_phesant.ht', _read_if_exists=True and not force_overwrite_intermediate)
    all_phenos = list(ht.row_value)
    ht = ht.select(
        **{code: [ht[x] for x in all_phenos if x.startswith(f'x{loc}')] for code, loc in code_locations.items()})
    ht = ht.annotate(**{code: ht[code].filter(lambda x: hl.is_defined(x)) for code in code_locations})
    all_codes = hl.sorted(hl.array(ht.aggregate(
        hl.agg.explode(lambda c: hl.agg.collect_as_set(c),
                       hl.flatmap(lambda x: x, [ht[code] for code in code_locations])),
        _localize=False)))
    ht = ht.select(
        bool_codes=all_codes.map(lambda x: hl.struct(**{code: ht[code].contains(x) for code in code_locations})))
    ht = ht.annotate_globals(all_codes=all_codes.map(lambda x: hl.struct(icd_code=x)))
    mt = ht._unlocalize_entries('bool_codes', 'all_codes', ['icd_code'])
    coding_ht = hl.import_table(icd_codings_path, impute=True, key='coding')
    return mt.annotate_cols(**coding_ht[mt.col_key]).annotate_globals(code_locations=code_locations)


def read_covariate_data(pre_phesant_data_path):
    ht = hl.import_table(pre_phesant_data_path, impute=True, min_partitions=100, missing='', key='userId')
    columns = {
        'sex': 'x22001_0_0',
        'age': 'x21022_0_0'
    }
    columns.update(**{f'pc{i}': f'x22009_0_{i}' for i in range(1, 41)})
    return ht.select(*columns.values()).rename({v: k for k, v in columns.items()}).annotate_globals(coding_source=columns)
