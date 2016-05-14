from __future__ import print_function
import argparse
import gzip
import itertools as it
from collections import defaultdict, Counter
from glob import glob
from os import path
from pysam import AlignmentFile as Samfile
import objgraph

try:
    from functools import reduce
    from operator import mul
except ImportError as exc:
    # We better hope we're in Python 2.
    print(exc)

MAX_SEQS_PER_READ = 1024

def product(iterable):
    return reduce(mul, iterable, 1)

def get_snps(snpdir):
    snp_dict = defaultdict(dict)
    if path.exists(path.join(snpdir, 'all.txt.gz')):
        print("Loading snps from consolidated file")
        for line in gzip.open(path.join(snpdir, 'all.txt.gz'), 'rt', encoding='ascii'):
            chrom, pos, ref, alt = line.split()
            pos = int(pos) - 1
            snp_dict[chrom][pos] = "".join([ref, alt])
        return snp_dict
    for fname in glob(path.join(snpdir, '*.txt.gz')):
        print("Loading snps from ", fname)
        chrom = path.basename(fname).split('.')[0]
        i = -1
        for i, line in enumerate(gzip.open(fname, 'rt', encoding='ascii')):
            pos, ref, alt = line.split()
            pos = int(pos) - 1
            snp_dict[chrom][pos] = "".join([ref, alt])
    return snp_dict

def get_indels(snp_dict):
    indel_dict = defaultdict(lambda: defaultdict(bool))
    for chrom in snp_dict:
        for pos, alleles in snp_dict[chrom].items():
            if ('-' in alleles) or (max(len(i) for i in alleles) > 1):
                indel_dict[chrom][pos] = True
    return indel_dict

RC_TABLE = {
    ord('A'):ord('T'),
    ord('T'):ord('A'),
    ord('C'):ord('G'),
    ord('G'):ord('C'),
}

def reverse_complement(seq):
    return seq.translate(RC_TABLE)[::-1]


def get_dual_read_seqs(read1, read2, snp_dict, indel_dict, dispositions):
    """ For each pair of reads, get all concordant SNP substitutions

    Note that if the reads overlap, the matching positions in read1 and read2
    will get the same subsitution as each other.
    """
    seq1 = read1.seq
    seq2 = read2.seq
    seqs1, seqs2 = [read1.seq], [read2.seq]

    chrom = read1.reference_name
    snps = {}
    read_posns = defaultdict(lambda: [None, None])

    for (read_pos1, ref_pos) in read1.get_aligned_pairs(matches_only=True):
        if indel_dict[chrom][ref_pos]:
            dispositions['toss_indel'] += 1
            return [[], []]
        if ref_pos in snp_dict[chrom]:
            snps[ref_pos] = snp_dict[chrom][ref_pos]
            read_posns[ref_pos][0] = read_pos1

    for (read_pos2, ref_pos) in read2.get_aligned_pairs(matches_only=True):
        if indel_dict[chrom][ref_pos]:
            dispositions['toss_indel'] += 1
            return [[], []]
        if ref_pos in snp_dict[chrom]:
            snps[ref_pos] = snp_dict[chrom][ref_pos]
            read_posns[ref_pos][1] = read_pos2

    if product(len(i) for i in snps.values()) > MAX_SEQS_PER_READ:
        dispositions['toss_manysnps'] += 1
        return [[], []]

    for ref_pos in snps:
        alleles = snps[ref_pos]
        pos1, pos2 = read_posns[ref_pos]
        new_seqs1 = []
        new_seqs2 = []
        if pos1 is None:
            for allele in alleles:
                if allele == seq2[pos2]:
                    continue
                for seq1, seq2 in zip(seqs1, seqs2):
                    new_seqs1.append(seq1)
                    new_seqs2.append(''.join([seq2[:pos2], allele, seq2[pos2+1:]]))

        elif pos2 is None:
            for allele in alleles:
                if allele == seq1[pos1]:
                    continue
                for seq1, seq2 in zip(seqs1, seqs2):
                    new_seqs1.append(''.join([seq1[:pos1], allele, seq1[pos1+1:]]))
                    new_seqs2.append(seq2)
        else:
            if seq1[pos1] != seq2[pos2]:
                dispositions['toss_anomalous_phase'] += 1
                return [[], []]
            for allele in alleles:
                if allele == seq2[pos2]:
                    continue
                for seq1, seq2 in zip(seqs1, seqs2):
                    new_seqs1.append(''.join([seq1[:pos1], allele, seq1[pos1+1:]]))
                    new_seqs2.append(''.join([seq2[:pos2], allele, seq2[pos2+1:]]))
        seqs1.extend(new_seqs1)
        seqs2.extend(new_seqs2)

    if len(seqs1) == 1:
        dispositions['no_snps'] += 1
    else:
        dispositions['has_snps'] += 1
    return seqs1, seqs2

def get_read_seqs(read, snp_dict, indel_dict, dispositions):
    num_snps = 0
    seqs = [read.seq]

    chrom = read.reference_name
    for (read_pos, ref_pos) in read.get_aligned_pairs(matches_only=True):
        if ref_pos is None:
            continue
        if indel_dict[chrom][ref_pos]:
            dispositions['toss_indel'] += 1
            return []
        if len(seqs) > MAX_SEQS_PER_READ:
            dispositions['toss_manysnps'] += 1
            return []

        if ref_pos in snp_dict[chrom]:
            read_base = read.seq[read_pos]
            if read_base in snp_dict[chrom][ref_pos]:
                dispositions['ref_match'] += 1
                num_snps += 1
                for new_allele in snp_dict[chrom][ref_pos]:
                    if new_allele == read_base:
                        continue
                    for seq in list(seqs):
                        # Note that we make a copy up-front to avoid modifying
                        # the list we're iterating over
                        new_seq = seq[:read_pos] + new_allele + seq[read_pos+1:]
                        seqs.append(new_seq)
            else:
                dispositions['no_match'] += 1
        else:
            # No SNP
            pass
    if len(seqs) == 1:
        dispositions['no_snps'] += 1
    else:
        dispositions['has_snps'] += 1
    return seqs

def assign_reads(insam, snp_dict, indel_dict, is_paired=True):
    fname = insam.filename
    if isinstance(fname, bytes):
        fname = fname.decode('ascii')
    basename = fname.rsplit('.', 1)[0]
    keep = Samfile('.'.join([basename, 'keep.bam']),
                   'wb',
                   template=insam)
    remap_bam = Samfile('.'.join([basename, 'to.remap.bam']),
                        'wb',
                        template=insam)
    dropped_bam = Samfile('.'.join([basename, 'dropped.bam']),
                          'wb',
                          template=insam)
    if is_paired:
        fastqs = [
            gzip.open('.'.join([basename, 'remap.fq1.gz']), 'wt'),
            gzip.open('.'.join([basename, 'remap.fq2.gz']), 'wt'),
        ]
    else:
        fastqs = [gzip.open('.'.join([basename, 'remap.fq.gz']), 'wt'),]
    unpaired_reads = [{}, {}]
    read_results = Counter()
    remap_num = 1
    for i, read in enumerate(insam):
        if i % 1000 == 0:
            print("Unpaired reads: ", len(unpaired_reads[0]), len(unpaired_reads[1]))
        if not is_paired:
            read_seqs = get_read_seqs(read, snp_dict, indel_dict, read_results)
            write_read_seqs([(read, read_seqs)], keep, remap_bam, fastqs)
        elif read.is_proper_pair:
            slot_self = read.is_read2 # 0 if is_read1, 1 if read2
            slot_other = read.is_read1
            if read.qname in unpaired_reads[slot_other]:
                both_reads = [None, None]
                both_reads[slot_self] = read
                both_reads[slot_other] = unpaired_reads[slot_other].pop(read.qname)
                both_seqs = get_dual_read_seqs(both_reads[0], both_reads[1],
                                               snp_dict, indel_dict, read_results)
                both_read_seqs = list(zip(both_reads, both_seqs))
                remap_num += write_read_seqs(both_read_seqs, keep, remap_bam,
                                             fastqs, dropped_bam, remap_num)
            else:
                unpaired_reads[slot_self][read.qname] = read
        else:
            read_results['not_proper_pair'] += 1
            # Most tools assume reads are paired and do not check IDs. Drop it out.
            continue
    print()
    print(len(unpaired_reads[0]), len(unpaired_reads[1]))
    print(read_results)


def write_read_seqs(both_read_seqs, keep, remap_bam, fastqs, dropped=None, remap_num=0):
    reads, seqs = zip(*both_read_seqs)
    assert len(reads) == len(fastqs)

    num_seqs = product(len(r[1]) for r in both_read_seqs)
    if num_seqs == 0 or num_seqs > MAX_SEQS_PER_READ:
        if dropped is not None:
            for read in reads:
                dropped.write(read)
            return 0
        else:
            return 0
    elif num_seqs == 1:
        for read, seqs in both_read_seqs:
            keep.write(read)
    else:
        assert len(reads) > 0
        for read in reads:
            remap_bam.write(read)
        left_pos = min(r.pos for r in reads)
        right_pos = max(r.pos for r in reads)
        loc_line = '{}:{}:{}:{}:{}'.format(
            remap_num,
            read.reference_name,
            left_pos,
            right_pos,
            num_seqs-1,
        )

        if left_pos == 16053407:
            print(seqs)
        first = True
        # Some python fanciness to deal with single or paired end reads (or
        # n-ended reads, if such technology ever happens.
        for read_seqs in it.product(*seqs):
            if first:
                first = False
                continue
            for seq, read, fastq in zip(read_seqs, reads, fastqs):
                fastq.write(
                    "@{loc_line}\n{seq}\n+{loc_line}\n{qual}\n"
                    .format(
                        loc_line=loc_line,
                        seq=reverse_complement(seq) if read.is_reverse else seq,
                        qual=read.qual)
                    )
        return 1
    return 0



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--paired_end",
                        action='store_true',
                        dest='is_paired_end', default=False,
                        help=('Indicates that reads are '
                              'paired-end (default is single).'))

    parser.add_argument("-s", "--sorted",
                        action='store_true', dest='is_sorted', default=False,
                        help=('Indicates that the input bam file'
                              ' is coordinate sorted (default is False)'))


    parser.add_argument("infile", type=Samfile, help=("Coordinate sorted bam "
                                                      "file."))
    snp_dir_help = ('Directory containing the SNPs segregating within the '
                    'sample in question (which need to be checked for '
                    'mappability issues).  This directory should contain '
                    'sorted files of SNPs separated by chromosome and named: '
                    'chr<#>.snps.txt.gz. These files should contain 3 columns: '
                    'position RefAllele AltAllele')

    parser.add_argument("snp_dir", action='store', help=snp_dir_help)

    options = parser.parse_args()

    SNP_DICT = get_snps(options.snp_dir)
    INDEL_DICT = get_indels(SNP_DICT)

    print("Done with SNPs")

    assign_reads(options.infile, SNP_DICT, INDEL_DICT, options.is_paired_end)
