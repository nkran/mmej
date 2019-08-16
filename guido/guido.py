import os
import re
import math
import argparse
import itertools
import log
import vcf
import gffutils

from Bio import SeqIO

from output import save_guides_list, save_guides_list_simple, save_detailed_list, save_detailed_list_simple, save_to_bed
from off_targets import run_bowtie, off_target_evaluation
from request import request_region_sequence, request_gene_sequence, request_region, request_feature, request_var_info


logger = log.createCustomLogger('root')
ROOT_PATH = os.path.abspath(os.path.dirname(__file__))


def break_dict(sequence, pams, pam_len, max_flanking_length, strand):
    '''
    Create a list of dictionaries of all PAMs with information about:
    position ('break'), pam sequence ('pam'), MMEJ search window ('rel_break' / 'seq'), gRNA ('guide'), and strand ('strand')
    '''
    
    breaks_list = []
    
    for pam in pams:
        
        break_dict = {}
        
        br = pam - pam_len
        left = br - max_flanking_length
        if left < 0:
            left = 0
        right = br + max_flanking_length
        
        break_dict['rel_break'] = br - left
        break_dict['break'] = br
        break_dict['seq'] = sequence[left:right]
        break_dict['pam'] = sequence[pam:pam+pam_len]
        break_dict['guide'] = sequence[pam-20:pam+pam_len]
        if strand == '+':
            break_dict['strand'] = '+'
        elif strand == '-':
            break_dict['strand'] = '-'
        
        breaks_list.append(break_dict)
    
    return breaks_list

def find_breaks(sequence, min_flanking_length, max_flanking_length, pam):
    '''
    Find Cas9-specific PAM motifs on both strands of a given sequence
    Assumes SpCas9 / 'NGG'-motif by default
    Keep only those which are more than 30 bp downstream
    '''
    
    logger.info('Finding PAMs ...')
    
    iupac_dict = {'A':'A',
                  'C':'C',
                  'G':'G',
                  'T':'T',
                  'R':'[AG]',
                  'Y':'[CT]',
                  'S':'[GC]',
                  'W':'[AT]',
                  'K':'[GT]',
                  'M':'[AC]',
                  'B':'[CGT]',
                  'D':'[AGT]',
                  'H':'[ACT]',
                  'V':'[ACG]',
                  'N':'[ACGT]'}
    iupac_pam = ''.join([iupac_dict[letter] for letter in pam])
    
    complement = {'A': 'T', 'C': 'G', 'G': 'C', 'T': 'A'}
    rev_seq = ''.join([complement[base] for base in sequence[::-1]])
    
    pams = [m.start() for m in re.finditer(r'(?=(%s))' % iupac_pam, sequence) if m.start(0) - min_flanking_length > 0 and m.end(0) + min_flanking_length < len(sequence)]
    rev_pams = [m.start() for m in re.finditer(r'(?=(%s))' % iupac_pam, rev_seq) if m.start(0) - min_flanking_length > 0 and m.end(0) + min_flanking_length < len(rev_seq)]
    pam_len = len(pam)
    
    all_breaks_list = break_dict(sequence, pams, pam_len, max_flanking_length, '+') + break_dict(rev_seq, rev_pams, pam_len, max_flanking_length, '-')
    
    return all_breaks_list


def get_cut_sites(region, min_flanking_length, max_flanking_length, pam):
    '''
    Get cutsites for positive and negative strand in the context
    of provided genomic region.
    '''

    logger.info('Analysing sequence ...')

    chromosome, start, end, seq = region
    cuts = find_breaks(seq, min_flanking_length, max_flanking_length, pam)

    for cut in cuts:
        break_abs = cut['break'] + start

        cut.update({'break_abs': break_abs})
        cut.update({'guide_loc': (chromosome, break_abs - 17, break_abs + 3 + len(pam))})

    return cuts


def find_microhomologies(left_seq, right_seq):
    '''
    Start with predefined k-mer length and extend it until it finds more
    than one match in sequence.
    '''

    # kmers list
    kmers = []

    # k-mer starting length
    min_kmer_length = 2

    # expand k-mer length
    for k in reversed(xrange(min_kmer_length, len(left_seq))):

        # iterate through sequence
        for i in range(len(left_seq) - k + 1):
            kmer = left_seq[i:i+k]

            if kmer in right_seq:
                kmers.append(kmer)

    return kmers


def simulate_end_joining(cut_list, length_weight):
    '''
    Simulates end joining with microhomologies
    '''

    logger.info('Simulating MMEJ ...')

    for i, cut in enumerate(cut_list):

        br = cut['rel_break']
        seq = cut['seq']

        # create list for storing MH patterns
        cut.update({'pattern_list': []})

        # split sequence at the break
        left_seq = seq[:br]
        right_seq = seq[br:]

        # find patterns in both sequences
        patterns = find_microhomologies(left_seq, right_seq)

        # iterate through patterns
        for pattern in patterns:
            p = re.compile(pattern)

            # find positions of patterns in each sequence
            left_positions = [m.start() for m in p.finditer(left_seq)]
            right_positions = [m.start() for m in p.finditer(right_seq)]

            # GC count for pattern
            pattern_GC = len(re.findall('G', pattern)) + len(re.findall('C', pattern))

            # get combinations
            pos_combinations = list(itertools.product(left_positions, right_positions))

            # generate microhomology for every combination
            for combination in pos_combinations:
                # save output to dict
                pattern_dict = {}

                # left side
                left_seq_pos = combination[0]
                left_deletion_length = len(left_seq) - left_seq_pos

                # right side
                right_seq_pos = combination[1]
                right_deletion_length = right_seq_pos

                # deletion length and sequence
                deletion_length = left_deletion_length + right_deletion_length
                deletion_seq = left_seq[left_seq_pos:] + right_seq[:right_seq_pos]

                # score pattern
                length_factor =  round(1 / math.exp((deletion_length) / (length_weight)), 3)
                pattern_score = 100 * length_factor * ((len(pattern) - pattern_GC) + (pattern_GC * 2))

                # frame shift
                if deletion_length % 3 == 0:
                    frame_shift = "-"
                else:
                    frame_shift = "+"

                # create dictionary
                pattern_dict['left'] = left_seq[:left_seq_pos] + '-' * left_deletion_length
                pattern_dict['left_seq'] = left_seq[:left_seq_pos]
                pattern_dict['left_seq_position'] = left_seq_pos
                pattern_dict['right'] = '+' * right_deletion_length + right_seq[right_seq_pos:]
                pattern_dict['right_seq'] = right_seq[right_seq_pos:]
                pattern_dict['right_seq_position'] = left_seq_pos + len(deletion_seq)
                pattern_dict['pattern'] = pattern
                pattern_dict['pattern_score'] = pattern_score
                pattern_dict['deletion_seq'] = deletion_seq
                pattern_dict['frame_shift'] = frame_shift

                # add to list
                cut['pattern_list'].append(pattern_dict)

        # remove duplicates and sub microhomologies
        pattern_list_filtered = []

        pattern_list = [dict(t) for t in set([tuple(sorted(pattern_dict.items())) for pattern_dict in cut['pattern_list']])]
        for pattern_dict in sorted(pattern_list, key=lambda x: x['pattern_score']):

            pass_array = []

            # iterate over previously saved patterns
            for x in pattern_list:

                # if the pattern is substring of any previous pattern
                if pattern_dict['pattern'] in x['pattern'] and pattern_dict['pattern'] != x['pattern']:

                    # get offset position of substring
                    offset_pattern = x['pattern'].find(pattern_dict['pattern'])
                    offset_left = pattern_dict['left_seq_position'] - (x['left_seq_position'] + offset_pattern)
                    offset_right = pattern_dict['right_seq_position'] - (x['right_seq_position'] + offset_pattern)

                    if offset_left == 0 and offset_right == 0:
                        pass_array.append(False)
                    else:
                        pass_array.append(True)
                else:
                    pass_array.append(True)

            # keep only unique mh patterns
            if all(pass_array):
                pattern_list_filtered.append(pattern_dict)

        cut.update({'pattern_list': pattern_list_filtered})

    return cut_list


def evaluate_guides(cut_sites, n_patterns, variants):
    '''
    Score guides and include information about SNPs and out-of-frame deletions
    '''
    guides = []

    logger.info('Evaluating guides ...')
    to_remove = []

    for i, cut_site in enumerate(cut_sites):
        
        if 'N' in cut_site['seq']:
            to_remove.append(i)
        else:
            guide = {}
            score = 0
            oof_score = 0

            # sort by pattern score
            sorted_pattern_list = sorted(cut_site['pattern_list'], key=lambda x: x['pattern_score'], reverse=True)[:n_patterns]
            guide_seq = cut_site['guide']

            chromosome, guide_start, guide_end = cut_site['guide_loc']

            # calculate SNP penalty
            snp_score = 0
            variants_in_guide = []

            if variants:
                for var in variants:
                    if guide_start <= var['start'] and guide_end >= var['end']:
                        variants_in_guide.append(var)

            if variants_in_guide:
                # wt_prob = reduce(lambda x, y: x*y, [1 - sum(v.aaf) for v in variants_in_guide if v.aaf])
                wt_prob = len(variants_in_guide)
            else:
                wt_prob = 1

            # calculate scores for MH in cut site
            for pattern_dict in sorted_pattern_list:
                if pattern_dict['frame_shift'] == "+":
                    oof_score += pattern_dict['pattern_score']

                score += pattern_dict['pattern_score']

            complete_score = oof_score / score * 100

            cut_site.update({'complete_score': complete_score})
            cut_site.update({'sum_score': score})
            cut_site.update({'variants': variants_in_guide})
            cut_site.update({'top_patterns': sorted_pattern_list})
            cut_site.update({'wt_prob': wt_prob})

    for index in sorted(to_remove, reverse=True):
        del cut_sites[index]

    return cut_sites


def parse_args():
    parser = argparse.ArgumentParser(description='Microhomology predictor.')
    
    parser.add_argument('--sequence-file', '-i', dest='sequence', help='File with the target sequence (TXT or FASTA).')
    parser.add_argument('--species', '-s', dest='species', help='Species. (Default: Anopheles gambiae)', default='anopheles_gambiae')
    parser.add_argument('--local', dest='local', nargs='?', const=True, help='Use genome information from a local folder.', default=False)
    parser.add_argument('--region', '-r', dest='region', help='Region in AgamP4 genome [2L:1530-1590].')
    parser.add_argument('--gene', '-G', dest='gene', help='Genome of interest (AgamP4.7 geneset).')
    parser.add_argument('--pam', '-P', dest='pam', help='Protospacer adjacent motif (IUPAC format)', default='NGG')
    parser.add_argument('--variants', '-v', nargs='?', const=True, dest='variants', help='VCF file with variants.')
    parser.add_argument('--max-flanking', '-M', type=int, dest='max_flanking_length', help='Max length of flanking region.', default=40)
    parser.add_argument('--min-flanking', '-m', type=int, dest='min_flanking_length', help='Min length of flanking region.', default=25)
    parser.add_argument('--length-weight', '-w', type=float, dest='length_weight', help='Length weight - used in scoring.', default=20.0)
    parser.add_argument('--n-patterns', '-p', type=int, dest='n_patterns', help='Number of MH patterns used in guide evaluation.', default=5)
    parser.add_argument('--output-folder', '-o', dest='output_folder', help="Output folder.")
    
    return parser.parse_args()


def define_genomic_region(chromosome, start, end):
    records = SeqIO.parse(os.path.join(ROOT_PATH, 'data', 'references', 'AgamP4.fa'), "fasta")
    reference = {}

    for record in records:
        reference[record.id] = record

    seq = str(reference[chromosome].seq.upper())[start:end]
    region = (chromosome, start, end, seq)

    return region


def annotate_guides(cut_sites, ann_db):
    '''
    Use GFF annotation to annotate guides
    '''

    for cut_site in cut_sites:
        location = cut_site['guide_loc']

        features = [f for f in ann_db.region(seqid=location[0], start=location[1], end=location[2])]
        cut_site.update({'annotation': features})

    return cut_sites


def main():

    ascii_header = r'''
                                                                
                    _|_|_|            _|        _|            
                  _|        _|    _|        _|_|_|    _|_|    
                  _|  _|_|  _|    _|  _|  _|    _|  _|    _|  
                  _|    _|  _|    _|  _|  _|    _|  _|    _|  
                    _|_|_|    _|_|_|  _|    _|_|_|    _|_|    
                                                                
                    '''

    print(ascii_header)
    logger.info("Let's dance!")

    args = parse_args()

    max_flanking_length = args.max_flanking_length
    min_flanking_length = args.min_flanking_length
    length_weight = args.length_weight

    # sequence input -------------------------------------------------------
    if args.region or args.gene:
        if args.local:
            if os.path.exists(os.path.join(ROOT_PATH, 'data', 'references', 'AgamP4.7')):
                ann_db = gffutils.FeatureDB(os.path.join(ROOT_PATH, 'data', 'references', 'AgamP4.7'), keep_order=True)
            else:
                # TODO
                logger.error('Genome sequence and annotation do not exist. Please, run guido-build-reference to add a new genome.')
                quit()
        else:
            ann_db = False

    if args.region and args.gene:
        logger.info('Please use only one option for genomic region selection. Use -r or -G.')
        quit()

    if args.region:
        '''
        Option -r: specify the region of interest where guides should be evaluated
        '''

        logger.info('Using reference genome for {}. Region: {}'.format(args.species, args.region))
        if args.local:
            chromosome = args.region.split(':')[0]
            start = int(args.region.split(':')[1].split('-')[0])
            end = int(args.region.split(':')[1].split('-')[1])
            
            region = define_genomic_region(chromosome, start, end)
        else:
            region = request_region_sequence(args.species, args.region)

    elif args.gene:
        '''
        Option -G: get genomic region from a gene name
        '''
        logger.info('Using AgamP4 reference genome. Gene: {}'.format(args.gene))
        if args.local:
            try:
                gene = ann_db[args.gene]
            except:
                logger.error('Gene not found: {}'.format(args.gene))
                quit()

            chromosome = gene.seqid
            start = gene.start
            end = gene.end

            region = define_genomic_region(chromosome, start, end)
        else:
            region = request_gene_sequence(args.species, args.gene)

    elif args.sequence:
        '''
        Option -i: read sequence from FASTA or txt file
        '''
        try:
            record = SeqIO.parse(args.sequence, "fasta").next()
            logger.info('Reading FASTA: {}'.format(args.sequence))
            seq = str(record.seq.upper())
        except:
            logger.info('Reading text sequence', args.sequence)
            with open(args.sequence, 'r') as f:
                seq = f.readline().strip().upper()

        region = ("sequence", 1, len(seq), seq)
    else:
        region = ('seq', 0, 0, False)

    '''
    Option -v: fetch variants for specified region from VectorBase
    '''
    if args.region and args.variants:
        variants = request_region(args.species, args.region, 'variation')

    elif args.gene and args.variants:
        variants = request_feature(args.species, args.gene, 'variation')

    else:
        variants = []

    if not args.region and not args.sequence and not args.gene:
        logger.error('Please define the region of interest (-r) or provide the sequence (-i). Use -h for help.')
        quit()

    if not args.output_folder:
        logger.error('No output folder selected. Please define it by using -o option.')
        quit()

    # create output dir if it doesn't exist
    if not os.path.exists(args.output_folder):
        os.makedirs(args.output_folder)

    # execute main steps
    cut_sites = get_cut_sites(region, min_flanking_length, max_flanking_length, args.pam)
    cut_sites = simulate_end_joining(cut_sites, length_weight)
    cut_sites = evaluate_guides(cut_sites, args.n_patterns, variants)

    if ann_db:
        cut_sites = annotate_guides(cut_sites, ann_db)

    target_dict = run_bowtie(cut_sites, os.path.join(ROOT_PATH, 'data', 'references', 'AgamP4'), args.output_folder)
    cut_sites = off_target_evaluation(cut_sites, target_dict)

    if args.sequence:
        # simple output
        save_guides_list_simple(cut_sites, args.output_folder, args.n_patterns)
        save_detailed_list_simple(cut_sites, args.output_folder, args.n_patterns)
    else:
        save_guides_list(cut_sites, args.output_folder, args.n_patterns)
        save_detailed_list(cut_sites, args.output_folder, args.n_patterns)
        save_to_bed(cut_sites, args.output_folder, args.n_patterns)
